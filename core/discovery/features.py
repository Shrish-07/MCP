"""Auditable daily technical features; fingerprints are descriptions only."""

import math
import statistics as stats
from core.exits.trail_manager import wilder_atr


def _std(values):
    return stats.pstdev(values) if len(values) >= 2 else 0.0


def _mean(values):
    return stats.mean(values) if values else None


def rsi14(closes):
    if len(closes)<15:return None
    changes=[b-a for a,b in zip(closes,closes[1:])]
    gains=[max(c,0) for c in changes];losses=[max(-c,0) for c in changes]
    gain,loss=stats.mean(gains[:14]),stats.mean(losses[:14])
    for g,l in zip(gains[14:],losses[14:]):gain,loss=(13*gain+g)/14,(13*loss+l)/14
    return 100-100/(1+gain/loss) if loss>0 else (100. if gain>0 else 50.)


def technical_features(bars, cfg):
    if len(bars) < 250:
        raise ValueError("at least 250 valid completed bars required")
    c = [b.close for b in bars]
    v = [b.volume for b in bars]
    returns = [b / a - 1 for a, b in zip(c, c[1:])]
    avg20 = [stats.mean(c[i - 19 : i + 1]) for i in range(len(c) - 20, len(c))]
    sma = stats.mean(c[-20:])
    prior_sma = stats.mean(c[-40:-20])
    r = {f"r{n}": c[-1] / c[-n - 1] - 1 for n in (5, 21, 63, 126)}
    blend = cfg.get("gap_weights", {}).get("momentum_blend", [0.6, 0.4])
    r["momentum_blend"] = blend[0] * r["r21"] + blend[1] * r["r63"]
    down_recent = [
        b.volume for a, b in zip(bars[-11:-1], bars[-10:]) if b.close < a.close
    ]
    down_prior = [
        b.volume for a, b in zip(bars[-21:-11], bars[-20:-10]) if b.close < a.close
    ]
    down_ratio = (
        (_mean(down_recent) / _mean(down_prior))
        if down_recent and down_prior and _mean(down_prior) > 0
        else None
    )
    rsi=rsi14(c)
    vol_ratio = (
        _std(returns[-10:]) / _std(returns[-30:-10])
        if _std(returns[-30:-10]) > 0
        else None
    )
    range_recent = (max(c[-10:]) - min(c[-10:])) / stats.mean(c[-10:])
    range_prior = (max(c[-30:-10]) - min(c[-30:-10])) / stats.mean(c[-30:-10])
    range_ratio = range_recent / range_prior if range_prior > 0 else None
    distance30 = 1 - c[-1] / max(c[-30:])
    slope = sma / prior_sma - 1
    distance20 = abs(c[-1] / sma - 1)
    thresholds = cfg.get("fingerprints", {})
    f1 = (
        slope >= thresholds.get("uptrend_slope_min", 0.01)
        and r["r63"] > 0
        and thresholds.get("pullback_min", 0.02)
        <= distance30
        <= thresholds.get("pullback_max", 0.10)
        and c[-1] >= sma * 0.97
    )
    f2 = (
        range_ratio is not None
        and vol_ratio is not None
        and down_ratio is not None
        and range_ratio <= thresholds.get("base_range_ratio_max", 0.8)
        and vol_ratio <= thresholds.get("base_vol_ratio_max", 0.8)
        and down_ratio <= thresholds.get("down_volume_ratio_max", 0.8)
    )
    f3 = (
        thresholds.get("exhaustion_min", 0.05)
        <= distance30
        <= thresholds.get("exhaustion_max", 0.15)
        and down_ratio is not None
        and down_ratio <= thresholds.get("down_volume_ratio_max", 0.8)
    )
    r.update(
        price=c[-1],
        high_252=max(b.high for b in bars[-252:]) if len(bars) >= 252 else None,
        high_runway=1 - c[-1] / max(b.high for b in bars[-252:])
        if len(bars) >= 252
        else None,
        closes_above_20dma_pct=sum(px > ma for px, ma in zip(c[-20:], avg20)) / 20,
        sma20=sma,
        sma20_slope_20=slope,
        up_volume_share=sum(
            b.volume for a, b in zip(bars[-21:-1], bars[-20:]) if b.close > a.close
        )
        / sum(v[-20:])
        if sum(v[-20:])
        else None,
        down_volume_ratio=down_ratio,
        abs_distance_20dma=distance20,
        rsi14=rsi,
        atr14=wilder_atr(bars),
        vol30_annual_pct=_std(returns[-30:]) * math.sqrt(252) * 100,
        dollar_volume30=stats.mean([b.close * b.volume for b in bars[-30:]]),
        range_contraction_ratio=range_ratio,
        volatility_contraction_ratio=vol_ratio,
        fingerprints={
            "F1_PULLBACK_UPTREND": f1,
            "F2_BASING": f2,
            "F3_DIP_EXHAUSTION": f3,
        },
        data_cutoff=bars[-1].timestamp,
        bar_count=len(bars),
        high_252_samples=min(252, len(bars)),
        feature_version="technical-v1",
    )
    return r
