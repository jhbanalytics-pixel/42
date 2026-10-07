"""The model 42 calls (Gemini on Vertex), the model ids for it, and what it costs.

MODEL_PROVIDER may be unset or gemini, the only provider; any other value fails. Model ids live in one place: the
constants below, each overridable by one environment variable. gemini-3.8-flash is priced at Google's LIST price (the 2026 introductory rate is a credit back on
the invoice, so the list price is the safe side for MODEL_DAILY_USD). Any other Gemini id has no price until
GEMINI_PRICE_INPUT_PER_M and GEMINI_PRICE_OUTPUT_PER_M (USD per million tokens) are set, and an unpriced model fails
before any call is made, so the daily cap can never run on a guess.

Vertex Priority PayGo is opt-in per process: GEMINI_PRIORITY=1 in a job's or service's own environment sends each of
its Gemini requests with PRIORITY_HEADER (core/llm/gemini.py), and since priority bills at a higher rate, price_for
then multiplies every Gemini price by GEMINI_PRIORITY_PRICE_MULT (default 1.8), so every cost estimate, reserve and
usage total of that process is on the priority price. Unset, nothing changes. A multiplier below 1 or not a number
leaves Gemini unpriced, so nothing spends on a guess.
"""

from __future__ import annotations

import math
import os

GEMINI = "gemini"
PROVIDERS = (GEMINI,)

GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"

# Google's list price, global endpoint, from the Vertex pricing page read 29 Sept 2026; thinking tokens bill as output.
GEMINI_LIST_PRICES = {"gemini-3.8-flash": {"input": 1.50, "output": 7.50, "cached": 0.15}}


# Priority PayGo: the request header Vertex reads, and the price multiplier when GEMINI_PRIORITY_PRICE_MULT is unset
# (Google bills priority at a higher rate than standard and publishes no fixed ratio; 1.8 is a third-party reading).
PRIORITY_HEADER = {"X-Vertex-AI-LLM-Shared-Request-Type": "priority"}
PRIORITY_PRICE_MULT = 1.8


class PriceUnset(KeyError):
    """A model was asked to spend before its price was set."""


def gemini_priority() -> bool:
    """True when this process's GEMINI_PRIORITY is 1 (or true, yes, on): its Gemini calls go out as Priority PayGo."""
    return (os.environ.get("GEMINI_PRIORITY") or "").strip().lower() in ("1", "true", "yes", "on")


def priority_price_mult() -> float:
    """GEMINI_PRIORITY_PRICE_MULT, else PRIORITY_PRICE_MULT; raises PriceUnset when it is set but not a number of at
    least 1, since a lower one would book less than priority bills."""
    value = (os.environ.get("GEMINI_PRIORITY_PRICE_MULT") or "").strip()
    if not value:
        return PRIORITY_PRICE_MULT
    try:
        mult = float(value)
    except ValueError:
        mult = math.nan
    if not math.isfinite(mult) or mult < 1:
        raise PriceUnset(f"GEMINI_PRIORITY_PRICE_MULT={value!r} must be a number of at least 1")
    return mult


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
    """{"input": usd_per_million, "output": usd_per_million}; raises PriceUnset when the model has no price yet. A
    Gemini price is multiplied by priority_price_mult() while gemini_priority() is on."""
    if is_gemini_model(model):
        price = _gemini_price(model)
        if gemini_priority():
            mult = priority_price_mult()
            price = {k: v * mult for k, v in price.items()}
        return price
    raise PriceUnset(f"{model} is not a priced model")


def _gemini_price(model: str) -> dict:
    try:
        inp, out = float(os.environ["GEMINI_PRICE_INPUT_PER_M"]), float(os.environ["GEMINI_PRICE_OUTPUT_PER_M"])
        return {"input": inp, "output": out, "cached": inp / 10}
    except (KeyError, ValueError):
        if model in GEMINI_LIST_PRICES:
            return GEMINI_LIST_PRICES[model]
        raise PriceUnset(f"{model} has no price: set GEMINI_PRICE_INPUT_PER_M and GEMINI_PRICE_OUTPUT_PER_M "
                         "(USD per million tokens, from the Vertex AI price page)") from None


def make_model(project: str | None = None, timeout_s: float | None = None):
    """The structured-output client: GeminiModel, with complete_json(system, user, schema, model, max_tokens).
    timeout_s bounds each HTTP attempt."""
    provider()
    from core.llm.gemini import GeminiModel

    kwargs = {"project": project} if project else {}
    return GeminiModel(**kwargs, timeout_s=timeout_s)
