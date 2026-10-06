"""Admission for explicitly bounded daily product composition."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from . import execution_approval
from .execution_generations import load_trusted_generation

POLICY_PATH = Path(__file__).resolve().parents[3] / "configs" / "daily_products.env"
POLICY_FLAGS = frozenset(
    {
        "PHASE_2_ENABLED",
        "SEED_INTELLIGENCE_ENABLED",
        "SEED_GRAPH_ENABLED",
        "SEED_CANDIDATES_ENABLED",
        "PAN_AFRICAN_ENABLED",
    }
)
REQUIRED_SOURCE_COLUMNS = (
    "id",
    "market",
    "platform",
    "source",
    "title",
    "text",
    "slang_terms",
    "topic_groups",
    "genz_score",
    "slang_score",
    "near_topic",
    "near_cosine",
    "published_at",
    "collected_at",
    "content_type",
    "author_handle",
    "author_handle_norm",
    "regional_score",
    "engagement_weighted",
    "creator_watchlist_score",
    "search_velocity_score",
)


@dataclass(frozen=True)
class ProductAdmission:
    manifest: object
    approval: object
    consumption: object
    generation: object


def load_product_policy():
    values = {}
    for line in POLICY_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator != "=" or name in values or value != "true":
            raise ValueError("products_policy_invalid")
        values[name] = True
    if set(values) != POLICY_FLAGS:
        raise ValueError("products_policy_invalid")
    return values


def admit_execution(receipt, *, require_model_budget=True):
    """Admit one consumed compose execution for product effects.

    ``require_model_budget`` keeps the full legacy product set, whose briefs are model
    backed, refused on an origin that funds no model call. The daily lifecycle passes
    False: a zero model origin then runs only the deterministic producers and records the
    model backed ones as unfunded, while a funded origin stays capped call by call.
    """
    execution = receipt.get("execution") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(execution, Mapping)
        or execution.get("stage") != "compose"
        or execution.get("mode") != "new_consume"
    ):
        raise ValueError("products_authority_unbound")
    authority = execution.get("authority")
    execution_approval._require_consumed_execution(
        authority, execution.get("consumption"), getattr(authority, "operation", None)
    )
    approval = authority.approval
    if type(approval) is not execution_approval.ExecutionApprovalV2:
        raise ValueError("products_approval_invalid")
    raw = approval.canonical_manifest_json.encode("utf-8")
    if sha256(raw).hexdigest() != approval.manifest_sha256:
        raise ValueError("products_manifest_differs")
    generation = load_trusted_generation(
        approval.origin_registry_sha256, approval.resource_manifest_sha256
    )
    execution_approval.canonical_approval_v2_bytes(
        approval,
        mode="new_consume",
        registry=generation.registry,
        expected_resource_manifest_sha256=generation.resource_manifest_sha256,
    )
    consumption = execution["consumption"]
    for field in (
        "approval_id",
        "manifest_sha256",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    ):
        if getattr(consumption, field) != getattr(approval, field):
            raise ValueError("products_consumption_differs")
    manifest = execution_approval.validate_execution_manifest(
        json.loads(raw), mode="new_consume", registry=generation.registry
    )
    if manifest.operation not in {"r3_apply", "daily_composition_apply"}:
        raise ValueError("products_operation_invalid")
    if manifest.project != "ogilvy-trends-v2" or manifest.datasets != ("trends_v2_staging",):
        raise ValueError("products_target_invalid")
    calls = manifest.limits["max_model_calls"]
    if type(calls) is not int or calls < 0 or (require_model_budget and calls == 0):
        raise ValueError("products_model_budget_unavailable")
    load_product_policy()
    if dict(manifest.input_artifacts).get("config") != sha256(POLICY_PATH.read_bytes()).hexdigest():
        raise ValueError("products_policy_not_admitted")
    return ProductAdmission(manifest, approval, execution["consumption"], generation)


def _serialized(value):
    if isinstance(value, Mapping):
        return {key: _serialized(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_serialized(item) for item in value]
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


class DailyProducts:
    def __init__(self, *, objects, build, now, io_factory=None, run_id=None, dynamic_meter=None):
        from .daily_product_completion import CompletionStore

        self.completion = CompletionStore(objects)
        self.policy_digest = sha256(POLICY_PATH.read_bytes()).hexdigest()
        self._build = build
        self._now = now
        self._io_factory = io_factory
        self._run_id = run_id
        self._dynamic_meter = dynamic_meter
        self._active = {}

    @property
    def dynamic_client(self):
        return self._dynamic_meter

    def pre_dispatch_refusal(self):
        if self._io_factory is None:
            return "products_history_policy_unavailable"
        if self._dynamic_meter is None:
            return "products_dynamic_metering_unavailable"
        return None

    def admit(self, *, entry, manifest, authority_receipt):
        from .brain_contract import canonical_digest
        from .capture_registry import validate_capture_entry

        admission = admit_execution(authority_receipt, require_model_budget=False)
        capture = validate_capture_entry(entry)
        cutoff = datetime.fromisoformat(manifest["cutoff_utc"])
        if (
            capture["completion_state"] != "succeeded"
            or capture["observation_window_end"] != cutoff
            or capture["policy_digest"] != manifest["profile"]["source_policy_digest"]
        ):
            raise ValueError("products_capture_differs")
        if self._build is None or any(
            self._build.get(name) != getattr(admission.manifest, name)
            for name in ("source_sha", "image_uri")
        ):
            raise ValueError("products_build_differs")
        # The packaged capture policy has no reviewed historical trend score or brief lane.
        if self._io_factory is None:
            raise ValueError("products_history_policy_unavailable")
        if self._dynamic_meter is None:
            raise ValueError("products_dynamic_metering_unavailable")
        key = manifest["operation_id"]
        state = self._active.get(key)
        binding = canonical_digest({"capture": _serialized(capture), "manifest": manifest})
        if state is not None and state["binding"] != binding:
            raise ValueError("products_attempt_conflict")
        if state is None:
            from .daily_product_io import ProductBudget
            from .persistence import validate_real_client

            validate_real_client(
                self._dynamic_meter,
                admission.manifest.project,
                writer_identity=admission.manifest.service_identity,
            )
            budget = ProductBudget(admission.manifest.limits)
            self._dynamic_meter.bind(budget)
            boundary = self._io_factory(admission=admission, capture=capture, cutoff=cutoff)
            self._active[key] = {
                "binding": binding,
                "admission": admission,
                "receipt": authority_receipt,
                "capture": capture,
                "cutoff": cutoff,
                "budget": budget,
                "boundary": boundary,
            }
        return self._active[key]

    def authority_receipt(self, run_id):
        if run_id != self._run_id or len(self._active) != 1:
            raise ValueError("products_run_unadmitted")
        return next(iter(self._active.values()))["receipt"]

    def run(self, *, entry, manifest, authority_receipt):
        from types import SimpleNamespace

        from src.analysis.gemini_client import GeminiClient

        from .daily_product_io import BoundedProductIO, MeteredModels

        state = self.admit(entry=entry, manifest=manifest, authority_receipt=authority_receipt)
        cutoff = state["cutoff"]
        day = (cutoff - timedelta(microseconds=1)).date()
        io = BoundedProductIO(
            state["boundary"],
            admission=state["admission"],
            budget=state["budget"],
            trend_date=day,
            ledger=self.completion,
            operation_id=manifest["operation_id"],
        )
        from .daily_product_io import DETERMINISTIC_PRODUCTS, MODEL_PRODUCTS

        # An earlier attempt's rows are replaced before any stage reads them, so no stage
        # skips work on them; an unfunded origin leaves the model backed tables untouched.
        io.replace_prior_writes(
            DETERMINISTIC_PRODUCTS + (MODEL_PRODUCTS if state["budget"].models_funded else ())
        )
        models = MeteredModels(
            state["boundary"].models,
            budget=state["budget"],
            completion=self.completion,
            operation_id=manifest["operation_id"],
        )
        client = GeminiClient(
            project=state["admission"].manifest.project, client=SimpleNamespace(models=models)
        )
        outcomes = run_legacy_products(
            io,
            enriched=state["boundary"].enriched,
            trend_date=day,
            cutoff=cutoff,
            scored_at=self._now(),
            gemini_client=client,
            models_funded=state["budget"].models_funded,
        )
        state["budget"].require_complete()
        state["io"] = io
        from .brain_contract import canonical_digest

        result = {"operation_id": manifest["operation_id"], "products": outcomes}
        state["result_digest"] = canonical_digest(result)
        return result

    def finish(self, result, *, dynamic_receipt):
        from .brain_contract import canonical_digest
        from .daily_product_completion import VERSION
        from .run_receipts import run_receipt_digest

        key = result["operation_id"]
        state = self._active[key]
        if canonical_digest(result) != state.get("result_digest"):
            raise ValueError("products_result_differs")
        if (
            dynamic_receipt.run_id != self._run_id
            or dynamic_receipt.signal_date != (state["cutoff"] - timedelta(microseconds=1)).date()
        ):
            raise ValueError("products_dynamic_identity_differs")
        dynamic = run_receipt_digest(dynamic_receipt)
        if dynamic_receipt.status != "completed" or not dynamic_receipt.complete_partitions:
            raise ValueError("products_dynamic_incomplete")
        self._dynamic_meter.require_complete(state["budget"])
        state["budget"].require_complete()
        view = state["io"].view_readback("v_desk_dynamic_signals_v2", run_id=self._run_id)
        if view["row_count"]:
            raise ValueError("products_dynamic_view_premature")
        return self.completion.record(
            {
                "schema_version": VERSION,
                "operation_id": key,
                "business_attempt_id": state["receipt"]["business_attempt_id"],
                "capture_digest": canonical_digest(_serialized(state["capture"])),
                "policy_digest": self.policy_digest,
                "source_sha": state["admission"].manifest.source_sha,
                "image_uri": state["admission"].manifest.image_uri,
                "cutoff_utc": state["cutoff"].isoformat(),
                "dynamic_receipt_digest": dynamic,
                "max_model_calls": state["admission"].manifest.limits["max_model_calls"],
                "products": {
                    **result["products"],
                    "v_desk_dynamic_signals_v2": {
                        "state": "awaiting_release",
                        "row_count": 0,
                        "output_digest": dynamic,
                        "readback_digest": view["readback_digest"],
                    },
                },
                "metering": state["budget"].metering(),
            }
        )

    def verify_release(self, *, run_id, operation_id, composition_digest, release_record_digest):
        from .brain_contract import canonical_digest

        if run_id != self._run_id or len(self._active) != 1:
            raise ValueError("products_release_reader_unavailable")
        state = next(iter(self._active.values()))
        outcome = state["io"].view_readback("v_desk_dynamic_signals_v2", run_id=run_id)
        record = self.completion.record_release(
            {
                "schema_version": "42_daily_products_release_v1",
                "operation_id": operation_id,
                "composition_digest": composition_digest,
                "release_record_digest": release_record_digest,
                "run_id": run_id,
                "view_row_count": outcome["row_count"],
                "view_rows_digest": outcome["readback_digest"],
            }
        )
        return self.completion.require_release(
            operation_id, expected_digest=canonical_digest(record)
        )


def validate_source_rows(frame, *, cutoff, trend_date):
    import pandas as pd

    missing = set(REQUIRED_SOURCE_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError("products_source_columns_missing:" + ",".join(sorted(missing)))
    collected = pd.to_datetime(frame["collected_at"], errors="coerce", utc=True)
    if (
        collected.isna().any()
        or (collected >= cutoff).any()
        or (collected.dt.date != trend_date).any()
        or not set(frame["market"]) <= {"za", "ng", "ke"}
        or frame["id"].isna().any()
        or frame.duplicated(["market", "id"]).any()
    ):
        raise ValueError("products_source_rows_invalid")
    return frame


def run_legacy_products(
    io, *, enriched, trend_date, cutoff, scored_at, gemini_client, models_funded=True
):
    from scripts.run_rss_now import _aggregate_by_topic, compute_trend_scores

    from src.analysis.generate_briefs import generate_briefs
    from src.analysis.generate_creator_briefs import persist_creator_briefs, project_creator_brief
    from src.analysis.generate_daily_summary import generate_daily_summary
    from src.analysis.generate_seed_intelligence import generate_seed_intelligence
    from src.analysis.pan_african import run_pan_african_stage
    from src.analysis.seed_candidates import run_seed_candidates_stage
    from src.analysis.seed_graph import build_seed_graph_rows
    from src.scoring.velocity import (
        compute_velocity_scores_for_today,
        compute_velocity_windows_for_today,
    )

    load_product_policy()
    validate_source_rows(enriched, cutoff=cutoff, trend_date=trend_date)
    counts = {}
    graph = []
    for market in ("za", "ng", "ke"):
        source = enriched.loc[enriched["market"] == market]
        grouped, _unclassified = _aggregate_by_topic(source, market)
        counts.update(grouped)
        graph.extend(build_seed_graph_rows(source, market, trend_date))
    velocity = compute_velocity_scores_for_today(
        counts, query_runner=io.query_runner, trend_date=trend_date
    )
    windows = compute_velocity_windows_for_today(
        counts, query_runner=io.query_runner, trend_date=trend_date
    )
    scores = compute_trend_scores(
        counts, trend_date, scored_at, velocity_scores=velocity, velocity_windows=windows
    )
    io.sink("trend_scores")(scores)
    io.sink("seed_graph")(graph)
    run_seed_candidates_stage(
        trend_date, client=io.client, dataset=io.dataset, row_sink=io.sink("seed_candidates")
    )
    if not models_funded:
        return _deterministic_products(io, trend_date=trend_date)
    report = generate_briefs(
        trend_date=trend_date,
        bq_client=io.client,
        dataset=io.dataset,
        gemini_client=gemini_client,
        row_sink=io.sink("trend_analysis"),
        usage_sink=io.sink("gemini_usage"),
    )
    if report.failures or (report.briefs and not report.persist_succeeded):
        raise ValueError("products_briefs_incomplete")
    briefs = {key: asdict(value) for key, value in report.briefs.items()}
    by_topic = {(row["market"], row["query_group"]): row for row in scores}
    generate_daily_summary(
        trend_date=trend_date,
        briefs_by_topic=briefs,
        trend_scores_by_topic=by_topic,
        gemini_client=gemini_client,
        bq_client=io.client,
        dataset=io.dataset,
        row_sink=io.sink("daily_summary"),
        usage_sink=io.sink("gemini_usage"),
    )
    generate_seed_intelligence(
        trend_date=trend_date,
        briefs_by_topic=briefs,
        trend_scores_by_topic=by_topic,
        gemini_client=gemini_client,
        bq_client=io.client,
        dataset=io.dataset,
        row_sink=io.sink("seed_insights"),
        usage_sink=io.sink("gemini_usage"),
    )
    retained = io.read_rows("trend_analysis", trend_date=trend_date)
    creators = [project_creator_brief(row) for row in retained]
    persist_creator_briefs(
        creators,
        row_sink=io.sink("creator_briefs"),
        row_reader=lambda ids: io.read_rows("creator_briefs", identifiers=ids),
    )
    run_pan_african_stage(
        trend_date,
        client=io.client,
        dataset=io.dataset,
        row_sink=io.sink("pan_african_stories"),
        enabled=True,
    )
    return {
        name: io.outcome(name)
        for name in (
            "seed_insights",
            "seed_candidates",
            "trend_analysis",
            "daily_summary",
            "v_seed_first_seen",
            "creator_briefs",
            "pan_african_stories",
        )
    }


def _deterministic_products(io, *, trend_date):
    """The products a zero model origin funds; the model backed ones are recorded unfunded."""
    from src.analysis.pan_african import run_pan_african_stage

    from .daily_product_io import MODEL_PRODUCTS

    run_pan_african_stage(
        trend_date,
        client=io.client,
        dataset=io.dataset,
        row_sink=io.sink("pan_african_stories"),
        enabled=True,
    )
    return {
        name: io.unfunded(name) if name in MODEL_PRODUCTS else io.outcome(name)
        for name in (
            "seed_insights",
            "seed_candidates",
            "trend_analysis",
            "daily_summary",
            "v_seed_first_seen",
            "creator_briefs",
            "pan_african_stories",
        )
    }
