import math
from utils.freshness import fresh_timestamp


def eligibility(features, quote, cfg):
    reasons = []
    for name in ("price", "dollar_volume30", "vol30_annual_pct", "bar_count"):
        if not isinstance(features.get(name), (int, float)) or not math.isfinite(
            features[name]
        ):
            reasons.append("invalid_" + name)
    if reasons:
        return reasons
    if features["price"] < cfg["min_price"]:
        reasons.append("price_floor")
    if features["dollar_volume30"] < cfg["min_dollar_volume"]:
        reasons.append("liquidity_floor")
    if features["vol30_annual_pct"] > cfg["max_vol_30d_pct_annualized"]:
        reasons.append("volatility_cap")
    if features["bar_count"] < cfg["min_bars"]:
        reasons.append("history_floor")
    if (
        quote is None
        or quote.price <= 0
        or not fresh_timestamp(quote.timestamp, 5 * 86400)
    ):
        reasons.append("stale_or_missing_quote")
    return reasons
