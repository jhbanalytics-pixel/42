import re
import time
from pathlib import Path

from src.api import bq, chat, main, persona_registry, research, seed_discover, synth


ROOT = Path(__file__).resolve().parents[2]
CURRENT_PRODUCT_PROSE = (
    "DESIGN_CONTEXT.md",
    "infra/cloud-run/deploy-open-intelligence-staging.sh",
    "src/api/creator.py",
    "src/api/chat.py",
    "src/api/bq.py",
    "src/api/events.py",
    "src/api/general_question_startup.py",
    "src/api/geo_blocklist.py",
    "src/api/lexicon.py",
    "src/api/research.py",
    "src/api/topic.py",
    "scripts/lp_qa.py",
    "scripts/smoke_test.sh",
    "scripts/verify_research_artifacts_iam.py",
    "docs/staging-deploy.md",
    "docs/staging-qa-checklist.md",
    "docs/lp-new-seeds-this-week-spec.md",
    "docs/V3-WOW-DEMO.md",
)
RETIRED_PRODUCT_NAME = re.compile(r"Listening Post|\bLP\b", re.IGNORECASE)


def test_only_audience_neutral_is_enabled():
    enabled = persona_registry.load_registry()
    all_personas = persona_registry.load_registry(include_disabled=True)

    assert list(enabled) == ["audience_neutral"]
    assert enabled["audience_neutral"]["enabled"] is True
    assert all_personas["digital_architect_genz"]["enabled"] is False
    assert all_personas["hustle_economy_genz"]["enabled"] is False
    assert all_personas["ai_adopter_genz"]["enabled"] is False


def test_backend_visible_product_name_is_42():
    assert main.app.title == "42"
    assert "Listening Post" not in main.CARD_LOCKED_HTML
    assert "Open it from the 42 interface" in main.CARD_LOCKED_HTML


def test_current_source_prose_uses_42_product_name():
    assert not (ROOT / "infra/cloud-run/deploy.sh").exists()
    assert all((ROOT / relative).is_file() for relative in CURRENT_PRODUCT_PROSE)
    violations = []
    for relative in CURRENT_PRODUCT_PROSE:
        for number, line in enumerate(
            (ROOT / relative).read_text(encoding="utf-8").splitlines(), start=1
        ):
            prose = line.replace("feat/lp-r-staging", "")
            if RETIRED_PRODUCT_NAME.search(prose):
                violations.append(f"{relative}:{number}")
    assert violations == []
    assert (ROOT / "docs/qa_log.md").read_text(encoding="utf-8").splitlines()[0] == (
        "# 42 QA log"
    )


def test_design_guidance_uses_neutral_evidence_contract():
    design = (ROOT / "DESIGN_CONTEXT.md").read_text(encoding="utf-8")
    assert design.startswith("# 42 Design Context")
    assert not re.search(r"\bGen(?:\s+|-)Z\b", design, re.IGNORECASE)
    assert "never audience evidence" in design
    assert "Explicit audience questions remain valid" in design
    assert "source, window, sample size, method and confidence" in design


def test_enabled_persona_has_no_product_or_vendor_selection():
    persona = persona_registry.load_registry()["audience_neutral"]
    picker = persona_registry.list_enabled_personas()[0]

    assert picker["product_frame"] == ""
    assert picker["product_frame_options"] == []
    assert "genz" not in repr(persona).lower()


def test_explicit_blank_product_frame_never_restores_a_persona_default():
    resolver = getattr(research, "_resolve_product_frame")
    persona = {"product_frame": "Legacy default"}

    assert resolver(None, persona) == "Legacy default"
    assert resolver("", persona) == ""
    assert resolver("  Mobile service  ", persona) == "Mobile service"


def test_chat_runtime_has_neutral_instruction_and_no_vendor_tools():
    active_text = "\n".join(
        [
            chat.SYSTEM_INSTRUCTION,
            repr(chat.TOOL_SCHEMAS),
            repr(sorted(chat._TOOLS)),
        ]
    ).lower()

    for retired in (
        "google x ogilvy",
        "google brief",
        "nano banana",
        "nanobanana",
        "lyria",
        "gen z",
    ):
        assert retired not in active_text


def test_research_gather_reads_only_the_pipeline_tables(monkeypatch):
    persona = persona_registry.load_registry()["audience_neutral"]
    monkeypatch.setattr(bq, "fetch_research_seeds", lambda *args, **kwargs: [])
    monkeypatch.setattr(bq, "fetch_research_digest", lambda *args, **kwargs: {})
    monkeypatch.setattr(bq, "fetch_topic_briefs_for_markets", lambda *args, **kwargs: [])
    monkeypatch.setattr(bq, "fetch_rising_search_terms_for_markets", lambda *args, **kwargs: [])
    monkeypatch.setattr(bq, "fetch_posts_and_comments_for_topics", lambda *args, **kwargs: [])
    monkeypatch.setattr(bq, "fetch_research_posts", lambda *args, **kwargs: [])
    monkeypatch.setattr(bq, "fetch_lexicon_rows_for_markets", lambda *args, **kwargs: [])

    degraded = []
    refs = research._parallel_gather(persona, ["za"], None, time.monotonic(), degraded, None)

    assert refs == []
    assert degraded == []


def test_standard_and_consolidated_research_prompts_are_neutral(monkeypatch):
    prompts = []
    monkeypatch.setattr(synth, "_call_model", lambda prompt: prompts.append(prompt) or "{}")
    base = {
        "persona_label": "Audience neutral",
        "markets": ["za"],
        "product_frame": "",
        "refs": [],
        "quality_gate": {},
    }

    assert synth.synthesize_research(base) is None
    standard_prompts = list(prompts)
    prompts.clear()
    assert synth.synthesize_consolidated_research({**base, "behaviour_sections": []}) is None
    consolidated_prompts = list(prompts)

    assert standard_prompts
    assert consolidated_prompts
    assert all(synth.AUDIENCE_EVIDENCE_RULE in prompt for prompt in standard_prompts)
    assert all(synth.AUDIENCE_EVIDENCE_RULE in prompt for prompt in consolidated_prompts)
    active_text = "\n".join(standard_prompts + consolidated_prompts).lower()
    for retired in ("google", "gemini", "lyria", "nano banana", "gen z"):
        assert retired not in active_text


def test_age_and_gender_claims_require_complete_measurement_receipt():
    claim = "Women aged 18 to 24 are the strongest audience for this behaviour."
    complete = {
        "audience_measurement": {
            "source": "measured panel",
            "window": {"start": "2026-08-01", "end": "2026-08-27"},
            "sample_size": 480,
            "method": "stratified survey",
            "confidence": 0.81,
        }
    }

    assert synth._audience_claim_supported(claim, []) is False
    for missing in ("source", "window", "sample_size", "method", "confidence"):
        receipt = {"audience_measurement": dict(complete["audience_measurement"])}
        receipt["audience_measurement"].pop(missing)
        assert synth._audience_claim_supported(claim, [receipt]) is False
    assert synth._audience_claim_supported(claim, [complete]) is True


def test_seed_discovery_never_emits_or_selects_genz(monkeypatch):
    monkeypatch.setattr(
        bq,
        "_seed_fit_dict",
        lambda row: {"co_occur": 0.0, "visual_audio": 0.0, "genz": 1.0, "slang": 0.8},
    )
    shaped = seed_discover._shape_candidate(
        {
            "candidate_id": "cand_1",
            "status": "pending",
            "lane": "daily",
            "source": "fixture",
            "evidence_topics": [],
            "sample_post_ids": [],
        }
    )

    assert "genz" not in shaped["fit"]
    assert "gen z" not in shaped["why"].lower()
    assert "slang signal" in shaped["why"].lower()


def test_research_artifact_uses_neutral_voice_read_key():
    artifact = synth.build_thin_research_doc(
        {
            "persona_label": "Audience neutral",
            "refs": [{"ref_type": "post", "text": "A cited post", "market": "za"}],
            "quality_gate": {"level": "thin"},
        }
    )

    grounded = artifact["json"]["grounded"]
    assert grounded["voice_read"] == ["A cited post"]
    assert "voice_of_genz" not in grounded
