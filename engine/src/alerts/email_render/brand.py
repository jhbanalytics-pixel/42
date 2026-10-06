"""Brand config loader for the PULSE v2 mailer.

A brand config is the white-label skin: name, lockup, palette, markets and
per-market colors. The same renderers wear a second client's skin by loading
a different config slug.
"""

from pathlib import Path

import yaml

_DIR = Path(__file__).resolve().parents[3] / "configs" / "mailer_brands"
_REQUIRED = {"name", "lockup", "palette", "markets", "market_colors"}


def load_brand(slug: str) -> dict:
    path = _DIR / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No mailer brand config: {path}")
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    missing = _REQUIRED - cfg.keys()
    if missing:
        raise ValueError(f"brand {slug!r} missing keys: {sorted(missing)}")
    return cfg
