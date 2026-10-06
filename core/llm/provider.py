"""The model 42 calls (Gemini on Vertex), the model ids for it, and what it costs.

MODEL_PROVIDER may be unset or gemini, the only provider; any other value fails. Model ids live in one place: the
constants below, each overridable by one environment variable. gemini-3.8-flash is priced at Google's LIST price (the 2026 introductory rate is a credit back on
the invoice, so the list price is the safe side for MODEL_DAILY_USD). Any other Gemini id has no price until
GEMINI_PRICE_INPUT_PER_M and GEMINI_PRICE_OUTPUT_PER_M (USD per million tokens) are set, and an unpriced model fails
before any call is made, so the daily cap can never run on a guess.
"""

from __future__ import annotations

import os

GEMINI = "gemini"
PROVIDERS = (GEMINI,)

GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"

# Google's list price, global endpoint, from the Vertex pricing page read 29 Sept 2026; thinking tokens bill as output.
GEMINI_LIST_PRICES = {"gemini-3.8-flash": {"input": 1.50, "output": 7.50, "cached": 0.15}}


class PriceUnset(KeyError):
    """A model was asked to spend before its price was set."""


def provider() -> str:
    value = (os.environ.get("MODEL_PROVIDER") or GEMINI).strip().lower()
    if value not in PROVIDERS:
        raise ValueError(f"MODEL_PROVIDER={value!r} is not one of {', '.join(PROVIDERS)}")
    return value


def is_gemini_model(model: str) -> bool:
    return str(model).startswith("gemini")


def thinking_headroom() -> int:
    """Output tokens Gemini may spend thinking, on top of the caller's max_tokens (thinking counts against the limit)."""
    return int(os.environ.get("GEMINI_THINKING_HEADROOM", "2000"))


def reserve_output(model: str, max_tokens: int) -> int:
    """The output tokens a cost estimate must reserve for a call sent with max_tokens: on Gemini, the allowance
    GeminiModel really sends, headroom included."""
    return max_tokens + (thinking_headroom() if is_gemini_model(model) else 0)


def gemini_model(role: str = "smart") -> str:
    """The Gemini model id: GEMINI_MODEL, and GEMINI_FAST_MODEL for the cheap roles (fast and video) when it is set."""
    main = os.environ.get("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL
    return (os.environ.get("GEMINI_FAST_MODEL") or main) if role in ("fast", "video") else main


def default_model(role: str = "smart") -> str:
    """The model id for a role (smart: writer, brief, Ask research; fast: support checks, enrich, fallback; video:
    video reading, the enrichment model reading media). MODEL_PROVIDER is checked first, so a bad value fails here."""
    provider()
    return gemini_model(role)


def price_for(model: str) -> dict:
    """{"input": usd_per_million, "output": usd_per_million}; raises PriceUnset when the model has no price yet."""
    if is_gemini_model(model):
        try:
            inp, out = float(os.environ["GEMINI_PRICE_INPUT_PER_M"]), float(os.environ["GEMINI_PRICE_OUTPUT_PER_M"])
            return {"input": inp, "output": out, "cached": inp / 10}
        except (KeyError, ValueError):
            if model in GEMINI_LIST_PRICES:
                return GEMINI_LIST_PRICES[model]
            raise PriceUnset(f"{model} has no price: set GEMINI_PRICE_INPUT_PER_M and GEMINI_PRICE_OUTPUT_PER_M "
                             "(USD per million tokens, from the Vertex AI price page)") from None
    raise PriceUnset(f"{model} is not a priced model")


def make_model(project: str | None = None, timeout_s: float | None = None):
    """The structured-output client: GeminiModel, with complete_json(system, user, schema, model, max_tokens).
    timeout_s bounds each HTTP attempt."""
    provider()
    from core.llm.gemini import GeminiModel

    kwargs = {"project": project} if project else {}
    return GeminiModel(**kwargs, timeout_s=timeout_s)
