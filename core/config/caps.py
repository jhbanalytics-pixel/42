"""Read the model spend cap and the video reading cap from core/config/caps.yaml at the time they are needed."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

CAPS_FILE = Path(__file__).resolve().parent / "caps.yaml"
SAST = timezone(timedelta(hours=2), "SAST")


def model_daily_usd(path=CAPS_FILE, *, now=None) -> float:
    """Return the model spend cap in USD for the supplied instant or current SAST date."""
    if now is None:
        now = datetime.now(SAST)
    elif now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")

    today = now.astimezone(SAST).date()
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["MODEL_DAILY_USD"]
    temporary = config.get("temporary")
    if temporary and date.fromisoformat(temporary["starts_on"]) <= today <= date.fromisoformat(temporary["ends_on"]):
        return float(temporary["amount"])
    return float(config["default"])


def video_daily(path=CAPS_FILE) -> dict:
    """VIDEO_DAILY as {"clips", "credits"}: the most clips video reading reads in a day and the SocialCrawl credits
    its own share may spend. A file without it reads as zero for both, so nothing is read or spent."""
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8")).get("VIDEO_DAILY") or {}
    return {"clips": int(config.get("clips") or 0), "credits": int(config.get("credits") or 0)}
