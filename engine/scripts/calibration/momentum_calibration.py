"""Momentum-in-composite calibration harness (read-only analysis).

Calibrates how a sustained-momentum signal should enter the live trend_score,
carving its weight from the existing velocity weight (Albert's call, 20 Jun) so
the velocity family keeps its total influence and the bundle stays at 1.0.

Method, all from stored data, no recompute from raw:
1. Pull trend_scores history. Every weighted signal component is a stored column,
   so the composite is reproducible. item_count gives the volume series.
2. Validate the recompute: rebuild the current composite from the components and
   confirm it matches the stored trend_score (proves the formula before we change
   weights). Also recompute velocity_score_7d/30d from item_count via the engine's
   own _normalise and confirm against the stored 20-Jun values.
3. Backtest a grid of carve splits x momentum definitions over the calibration
   window: tier-distribution stability, flash demotion, sustained promotion.

Read only. Prints a report, writes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.scoring.velocity import _normalise  # the engine's exact velocity formula
from src.utils.bigquery import get_client, get_dataset

# Live audience-neutral weights from configs/scoring.yaml.
W = {
    "velocity": 0.20,
    "diversity": 0.16,
    "engagement": 0.10,
    "creator_spread": 0.12,
    "regional_score": 0.12,
    "search_velocity_score": 0.15,
    "slang_score": 0.10,
    "tone_score": 0.05,
}
TIERS = {"Key": 0.45, "Emerging": 0.30, "Monitoring": 0.18}
CAL_DAYS = 21
HIST_DAYS = 40  # enough lead for a 30-day baseline before the window starts


def _composite(r: pd.Series, w: dict, momentum_signal: float | None, mom_weight: float) -> float:
    """Reproduce run_rss_now.compute_trend_scores, optionally carving momentum.

    Seven signals scaled by weight_scale (tone redistributed when tone_rows==0),
    plus tone. When mom_weight>0 the velocity weight is already reduced in `w`
    and momentum_signal rides at mom_weight, so the family total is unchanged.
    """
    signals = (
        r["velocity_score"] * w["velocity"]
        + r["diversity_score"] * w["diversity"]
        + r["engagement_score"] * w["engagement"]
        + r["creator_score"] * w["creator_spread"]
        + r["regional_score"] * w["regional_score"]
        + r["search_velocity_score"] * w["search_velocity_score"]
        + r["slang_score"] * w["slang_score"]
    )
    if mom_weight and momentum_signal is not None:
        signals += momentum_signal * mom_weight
    tone_nominal = w["tone_score"]
    if int(r.get("tone_rows") or 0) > 0 and pd.notna(r.get("tone_score")):
        comp = signals + float(r["tone_score"]) * tone_nominal
    else:
        remaining = 1.0 - tone_nominal
        comp = signals * (1.0 / remaining if remaining > 0 else 1.0)
    # Cross-source confirmation multiplier (channel families), capped, then 0..1.
    cdv = r.get("channel_diversity")
    cd = max(int(cdv) - 1, 0) if pd.notna(cdv) else 0
    mult = 1.0 + min(0.05 * cd, 0.15)
    return round(min(comp * mult, 1.0), 4)


def _tier(score: float) -> str:
    for name, thr in TIERS.items():
        if score >= thr:
            return name
    return "below"


def main() -> int:
    client = get_client()
    ds = get_dataset()
    proj = client.project
    sql = f"""
        SELECT trend_date, market, query_group, item_count, tone_rows,
               velocity_score, diversity_score, engagement_score, creator_score,
               regional_score, genz_score, watchlist_score, slang_score,
               search_velocity_score, tone_score, trend_score, channel_diversity,
               velocity_score_7d, velocity_score_30d
        FROM `{proj}.{ds}.trend_scores`
        WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {HIST_DAYS} DAY)
        ORDER BY market, query_group, trend_date
    """
    df = client.query(sql).result().to_dataframe()
    df["trend_date"] = pd.to_datetime(df["trend_date"])
    for c in df.columns:
        if c not in ("trend_date", "market", "query_group"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    print(
        f"pulled {len(df)} trend_scores rows over {HIST_DAYS} days, "
        f"{df['trend_date'].dt.date.nunique()} distinct dates"
    )

    # --- 1. validate the current composite recompute vs stored trend_score ---
    df["recomp_current"] = df.apply(lambda r: _composite(r, W, None, 0.0), axis=1)
    has_score = df["trend_score"].notna()
    err = (df.loc[has_score, "recomp_current"] - df.loc[has_score, "trend_score"]).abs()
    print("\n=== composite recompute validation (recomp vs stored trend_score) ===")
    print(f"  rows checked: {int(has_score.sum())}")
    print(
        f"  mean abs err: {err.mean():.4f}   max abs err: {err.max():.4f}   "
        f"median: {err.median():.4f}"
    )
    print(
        f"  within 0.01: {(err <= 0.01).mean() * 100:.1f}%   "
        f"within 0.03: {(err <= 0.03).mean() * 100:.1f}%"
    )

    # --- 2. backfill momentum windows from item_count, validate vs stored 20-Jun ---
    def windows(g: pd.DataFrame) -> pd.DataFrame:
        g = g.sort_values("trend_date").copy()
        ic = g["item_count"].astype(float)
        base7 = ic.shift(1).rolling(7, min_periods=1).mean()
        base30 = ic.shift(1).rolling(30, min_periods=1).mean()
        g["bf_v7"] = [
            _normalise(int(c), float(b) if pd.notna(b) else 0.0)
            for c, b in zip(ic, base7, strict=False)
        ]
        g["bf_v30"] = [
            _normalise(int(c), float(b) if pd.notna(b) else 0.0)
            for c, b in zip(ic, base30, strict=False)
        ]
        return g

    df = df.groupby(["market", "query_group"], group_keys=False).apply(windows)
    stored = df[df["velocity_score_7d"].notna()]
    if len(stored):
        d7 = (stored["bf_v7"] - stored["velocity_score_7d"]).abs()
        d30 = (stored["bf_v30"] - stored["velocity_score_30d"]).abs()
        print("\n=== momentum backfill validation (vs stored, where present) ===")
        print(
            f"  rows: {len(stored)}   7d mean abs err: {d7.mean():.4f} max {d7.max():.4f}"
            f"   30d mean abs err: {d30.mean():.4f} max {d30.max():.4f}"
        )

    # calibration window
    cutoff = df["trend_date"].max() - pd.Timedelta(days=CAL_DAYS)
    cal = df[df["trend_date"] > cutoff].copy()
    print(
        f"\ncalibration window: {cal['trend_date'].dt.date.min()} to "
        f"{cal['trend_date'].dt.date.max()}, {len(cal)} rows"
    )

    # momentum signal definitions (sustained = up on both windows)
    cal["mom_7d"] = cal["bf_v7"]
    cal["mom_min"] = cal[["bf_v7", "bf_v30"]].min(axis=1)
    cal["mom_mean"] = cal[["bf_v7", "bf_v30"]].mean(axis=1)

    # flash vs sustained labels for the separation test
    cal["is_flash"] = (cal["velocity_score"] >= 0.3) & (cal["mom_min"] <= 0.1)
    cal["is_sustained"] = (cal["velocity_score"] >= 0.2) & (cal["mom_min"] >= 0.2)

    cal["cur"] = cal.apply(lambda r: _composite(r, W, None, 0.0), axis=1)
    cal["cur_tier"] = cal["cur"].map(_tier)

    print("\n=== BACKTEST: carve-from-velocity grid ===")
    print(
        f"  flashes in window: {int(cal['is_flash'].sum())}   "
        f"sustained: {int(cal['is_sustained'].sum())}"
    )
    print(f"  current tiers: {cal['cur_tier'].value_counts().to_dict()}")
    print()
    header = f"  {'split(vel/mom)':>16} {'mom_def':>8} {'tier_moved':>10} "
    header += f"{'flash_dn%':>10} {'sustain_up%':>12} {'mean|dScore|':>12}"
    print(header)
    for mom_w in (0.05, 0.07, 0.10):
        vel_w = 0.20 - mom_w
        w2 = dict(W, velocity=vel_w)
        for mom_def in ("mom_7d", "mom_min", "mom_mean"):
            new = cal.apply(
                lambda r, w2=w2, mom_def=mom_def, mom_w=mom_w: _composite(r, w2, r[mom_def], mom_w),
                axis=1,
            )
            new_tier = new.map(_tier)
            moved = (new_tier != cal["cur_tier"]).mean() * 100
            dscore = (new - cal["cur"]).abs().mean()
            fl = cal["is_flash"]
            su = cal["is_sustained"]
            flash_dn = ((new - cal["cur"])[fl] < 0).mean() * 100 if fl.any() else float("nan")
            sustain_up = ((new - cal["cur"])[su] > 0).mean() * 100 if su.any() else float("nan")
            print(
                f"  {f'{vel_w:.2f}/{mom_w:.2f}':>16} {mom_def:>8} {moved:>9.1f}% "
                f"{flash_dn:>9.1f}% {sustain_up:>11.1f}% {dscore:>12.4f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
