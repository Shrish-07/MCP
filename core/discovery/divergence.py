"""Cross-sectional winsorization and a separately standardized raw gap."""

import math
import statistics as stats


def standardize(values, min_cohort=20):
    if len(values) < min_cohort or not all(math.isfinite(v) for v in values):
        raise ValueError("invalid/small standardization cohort")
    mean, sd = stats.mean(values), stats.pstdev(values)
    if sd <= 1e-12:
        raise ValueError("degenerate cross section")
    clipped = [max(mean - 3 * sd, min(mean + 3 * sd, v)) for v in values]
    center, scale = stats.mean(clipped), stats.pstdev(clipped)
    if scale <= 1e-12:
        raise ValueError("degenerate winsorized cohort")
    return [(v - center) / scale for v in clipped], {
        "method": "mean/std; clip at initial mean +/-3 population SD; recenter/rescale",
        "center": center,
        "scale": scale,
        "size": len(values),
        "clip_low": mean - 3 * sd,
        "clip_high": mean + 3 * sd,
    }


def score_fixed_reference(row, entry_evidence, cfg):
    """Comparable held gap against frozen entry-cohort transforms, never a new cohort."""

    def z(value, meta):
        return (
            max(meta["clip_low"], min(meta["clip_high"], value)) - meta["center"]
        ) / meta["scale"]

    component = {k: z(row[k], v) for k, v in entry_evidence["cohort"].items()}
    w = cfg["gap_weights"]
    raw = (
        w["sentiment_mean"] * component["sentiment_mean"]
        + w["velocity"] * component["news_velocity"]
        + w["delta"] * component["sentiment_delta"]
        - component["momentum_blend"]
        - w["recent_move"] * component["r5"]
    )
    return z(raw, entry_evidence["gap_standardization"])


def score_cohort(rows, cfg, reference):
    tickers = sorted(rows)
    minimum = cfg.get("min_cohort", 20)
    components = {}
    metadata = {}
    for field in (
        "sentiment_mean",
        "news_velocity",
        "sentiment_delta",
        "momentum_blend",
        "r5",
    ):
        z, meta = standardize([rows[t][field] for t in tickers], minimum)
        metadata[field] = meta
        components[field] = dict(zip(tickers, z))
    w = cfg["gap_weights"]
    output = {}
    for ticker in tickers:
        news = (
            w["sentiment_mean"] * components["sentiment_mean"][ticker]
            + w["velocity"] * components["news_velocity"][ticker]
            + w["delta"] * components["sentiment_delta"][ticker]
        )
        price = (
            components["momentum_blend"][ticker]
            + w["recent_move"] * components["r5"][ticker]
        )
        output[ticker] = dict(
            rows[ticker],
            ticker=ticker,
            news_score=news,
            price_score=price,
            raw_gap_score=news - price,
            z_components={k: v[ticker] for k, v in components.items()},
        )
    gaps, gap_meta = standardize([output[t]["raw_gap_score"] for t in tickers], minimum)
    for ticker, gap in zip(tickers, gaps):
        row = output[ticker]
        row.update(
            gap_z=gap,
            gap_reference=reference,
            cohort=metadata,
            gap_standardization=gap_meta,
        )
        fp = row["fingerprints"]
        arch = cfg["archetypes"]
        if gap <= arch["avoid_gap_z"]:
            label = "D_AVOID_SIDE"
        elif gap >= arch["lagged_gap_z"] and any(fp.values()):
            label = "A_LAGGED_GOOD_NEWS"
        elif fp.get("F2_BASING") and row["sentiment_delta"] >= arch["inflection_delta"]:
            label = "B_EARLY_INFLECTION"
        elif (
            row["sentiment_mean"] > 0
            and row["momentum_blend"] > 0
            and row.get("confirmed_catalyst")
            and row.get("high_runway") is not None
            and row["high_runway"] >= 0.03
        ):
            label = "C_CONFIRMED_DRIFT"
        else:
            label = None
        row["archetype"] = label
    ranked = sorted(tickers, key=lambda t: (output[t]["gap_z"], t))
    for i, t in enumerate(ranked):
        output[t]["gap_decile"] = min(10, int(i * 10 / len(ranked)) + 1)
    return output
