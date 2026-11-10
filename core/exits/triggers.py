"""Measured advisory triggers E5–E12; none can veto or delay a hard stop."""

import math
import statistics as stats
from datetime import datetime, timezone
from .exit_state import queue_review


def trading_sessions(entry, now=None):
    from utils.market_calendar import _NYSE

    if _NYSE is None or not entry:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        start = datetime.fromisoformat(entry).date()
        sessions = _NYSE.schedule(start_date=start, end_date=now.date())
        return max(
            0,
            len(
                [
                    d
                    for d, row in sessions.iterrows()
                    if row["market_close"].to_pydatetime() <= now
                ]
            )
            - 1,
        )
    except (ValueError, TypeError):
        return None


def gap_progress(entry, current, *, comparable=True):
    if (
        not comparable
        or entry is None
        or current is None
        or not all(math.isfinite(v) for v in (entry, current))
        or abs(entry) < 1e-6
    ):
        return None, False
    sign = 1 if entry > 0 else -1
    return max(0, min(1, 1 - sign * current / abs(entry))), sign * current < 0


def trend_break(bars, was_profitable, cfg):
    n = cfg.get("ma_days", 20)
    count = cfg.get("consecutive_closes", 2)
    if len(bars) < n + count - 1 or (
        cfg.get("only_if_was_profitable", True) and not was_profitable
    ):
        return None
    closes = [b.close for b in bars]
    measured = []
    for index in range(len(bars) - count, len(bars)):
        average = stats.mean(closes[index - n + 1 : index + 1])
        if closes[index] >= average:
            return None
        measured.append(
            {"date": bars[index].timestamp, "close": closes[index], "sma": average}
        )
    return {"completed_closes": measured}


def climax(bars, rsi, cfg):
    if len(bars) < 21 or rsi is None or rsi < cfg.get("rsi_min", 80):
        return None
    closes = [b.close for b in bars[-21:]]
    returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
    largest = max(returns)
    recent_largest = largest > 0 and any(v == largest for v in returns[-3:])
    sd = stats.pstdev(closes[-20:])
    sigma = (closes[-1] - stats.mean(closes[-20:])) / sd if sd > 0 else None
    if recent_largest or (
        sigma is not None and sigma >= cfg.get("sigma_above_20dma", 3)
    ):
        return {
            "rsi14": rsi,
            "largest_gain_in_last3": recent_largest,
            "sigma_above_sma20": sigma,
            "distribution": "last20 completed closes about their mean",
            "move_window_sessions": 20,
        }
    return None


def pead(event, history, cfg):
    required = (
        "actual_eps",
        "consensus_eps",
        "release_timestamp",
        "source",
        "age_sessions",
        "car_by_session",
        "data_cutoff",
    )
    if not event or any(event.get(k) is None for k in required):
        return {"available": False, "reason": "earnings/benchmark history missing"}
    try:
        release = datetime.fromisoformat(event["release_timestamp"])
        cutoff = datetime.fromisoformat(event["data_cutoff"])
        if (
            not release.tzinfo
            or not cutoff.tzinfo
            or cutoff < release
            or cutoff > datetime.now(timezone.utc)
        ):
            raise ValueError("invalid event data cutoff")
    except (ValueError, TypeError):
        return {"available": False, "reason": "invalid event timestamps"}
    consensus = event["consensus_eps"]
    if not math.isfinite(consensus) or abs(consensus) < 1e-6:
        return {"available": False, "reason": "consensus denominator unavailable"}
    surprise = (event["actual_eps"] - consensus) / abs(consensus)
    if surprise <= 0:
        return {
            "available": False,
            "reason": "positive long-equity drift hypothesis unavailable",
        }

    def bucket(s):
        return 0 if s < 0.05 else 1 if s < 0.10 else 2

    peers = [];seen=set()
    for h in history:
        try:
            available = datetime.fromisoformat(h["available_at"])
            if (
                available.tzinfo
                and available < release
                and h.get("source")
                and h.get("event_id") and h["event_id"] not in seen
                and h.get("surprise", -1) > 0
                and bucket(h["surprise"]) == bucket(surprise)
            ):
                peers.append(h)
                seen.add(h["event_id"])
        except (KeyError, ValueError, TypeError):
            continue
    if len(peers) < cfg.get("min_comparable_events", 30):
        return {
            "available": False,
            "reason": "insufficient prior comparable events",
            "sample_size": len(peers),
        }
    curve = {}
    for session in (1, 3, 5, 10, 15):
        sample = [h.get("car_by_session", {}).get(str(session)) for h in peers]
        if any(v is None or not math.isfinite(v) for v in sample):
            return {
                "available": False,
                "reason": "incomplete benchmark-adjusted curves",
            }
        curve[str(session)] = {
            "mean": stats.mean(sample),
            "n": len(sample),
            "approx_95pct": [
                stats.mean(sample)
                - 1.96 * stats.stdev(sample) / math.sqrt(len(sample)),
                stats.mean(sample)
                + 1.96 * stats.stdev(sample) / math.sqrt(len(sample)),
            ] if len(sample)>=2 else None,
        }
    expected = curve["15"]["mean"]
    if expected <= 1e-4:
        return {
            "available": False,
            "reason": "nonpositive/negligible same-direction expected drift",
        }
    age = event["age_sessions"]
    if age < cfg.get("min_sessions_after_earnings", 5):
        return {"available": True, "trigger": False, "expected_curve": curve}
    session = max(s for s in (1, 3, 5, 10, 15) if s <= min(age, 15))
    actual = event["car_by_session"].get(str(session))
    if actual is None or not math.isfinite(actual):
        return {"available": False, "reason": "current CAR unavailable"}
    fraction = actual / expected
    return {
        "available": True,
        "trigger": fraction + 1e-12 >= cfg.get("capture_fraction_threshold", 0.8)
        or age >= cfg.get("max_drift_sessions", 15),
        "surprise": surprise,
        "age_sessions": age,
        "actual_car": actual,
        "expected_remaining_drift": expected - actual,
        "expected_curve": curve,
        "captured_fraction": fraction,
        "sample_size": len(peers),
        "source": event["source"],
        "data_cutoff": event["data_cutoff"],
    }


def abnormal_curve(stock_returns, benchmark_returns):
    """Geometric cumulative stock minus benchmark returns at matched event sessions."""
    if len(stock_returns) != len(benchmark_returns) or len(stock_returns) < 15:
        raise ValueError("matched 15-session return histories required")
    stock = benchmark = 1.0
    curve = {}
    for session, (s, b) in enumerate(zip(stock_returns, benchmark_returns), 1):
        if not all(math.isfinite(v) and v > -1 for v in (s, b)):
            raise ValueError("invalid return")
        stock *= 1 + s
        benchmark *= 1 + b
        if session in (1, 3, 5, 10, 15):
            curve[str(session)] = stock - benchmark
    return curve


def nightly_signals(
    record,
    bars,
    features,
    news,
    settings,
    *,
    current_gap=None,
    comparable=False,
    pead_event=None,
    pead_history=(),
    best_candidate=None,
    now=None,
):
    cfg = settings["exits"]
    now = now or datetime.now(timezone.utc)
    signals = {}
    unavailable = {}
    trend = trend_break(
        bars, record.peak_price > record.entry_price, cfg["trend_break"]
    )
    record.trigger_memory["trend_break_active"] = bool(trend)
    if trend:
        signals["TREND_BREAK"] = trend
    exhausted = climax(bars, features.get("rsi14"), cfg["climax"])
    if exhausted:
        signals["CLIMAX"] = exhausted
        if record.atr14:
            record.trail_stop_px = max(
                record.trail_stop_px,
                record.peak_price
                - cfg["climax"]["tighten_atr_multiple"] * record.atr14,
            )
            record.effective_stop_px = max(
                record.effective_stop_px, record.trail_stop_px
            )
    delta = (news or {}).get("z_components", {}).get("sentiment_delta")
    reversal = bool(
        news
        and delta is not None
        and news.get("recent_negative_tone")
        and delta <= cfg["sentiment_reversal"]["delta_z"]
        and news.get("news_velocity", 0)
        >= cfg["sentiment_reversal"]["news_velocity_multiple"]
    )
    record.trigger_memory["sentiment_reversal_active"] = reversal
    if reversal:
        signals["SENTIMENT_REVERSAL"] = {
            "delta_z": delta,
            "news_velocity": news["news_velocity"],
            "velocity_definition": news.get("velocity_definition"),
        }
    elif delta is None:
        unavailable["sentiment_reversal"] = (
            "valid standardized sentiment/baseline missing"
        )
    progress, inverted = gap_progress(
        record.entry_gap_z, current_gap, comparable=comparable
    )
    record.gap_progress, record.gap_inverted = progress, inverted
    age = trading_sessions(record.entry_date, now) if record.entry_date_known else None
    pnl = bars[-1].close / record.entry_price - 1
    if progress is None:
        unavailable["gap_rules"] = (
            "entry anchor/current reference unavailable or incomparable"
        )
    else:
        remaining = (1 if record.entry_gap_z > 0 else -1) * current_gap
        if inverted or remaining <= cfg["gap_closure_z"]:
            signals["GAP_CLOSURE"] = {
                "remaining_gap_z": remaining,
                "inverted": inverted,
                "progress": progress,
            }
        dead = cfg["dead_money"]
        if (
            age is not None
            and age >= dead["trading_days"]
            and abs(pnl) < dead["unrealized_band_pct"]
            and progress < dead["gap_progress_min"]
        ):
            signals["DEAD_MONEY"] = {
                "sessions": age,
                "pnl": pnl,
                "gap_progress": progress,
            }
    from utils.freshness import fresh_timestamp
    if pead_event and not fresh_timestamp(pead_event.get("data_cutoff"),5*86400,now):
        pead_event=None
    measured = (
        pead(pead_event, pead_history, cfg["pead"])
        if cfg["pead"]["enabled"]
        else {"available": False, "reason": "disabled"}
    )
    if measured.get("trigger"):
        signals["PEAD_SPENT"] = measured
    if not measured["available"]:
        unavailable["PEAD"] = measured
    if (
        best_candidate
        and news
        and best_candidate.get("gap_reference") == news.get("gap_reference")
        and best_candidate["gap_z"] - news["gap_z"] >= cfg["rotation_margin_z"]
    ):
        signals["OPPORTUNITY_COST"] = {
            "candidate": best_candidate["ticker"],
            "candidate_gap_z": best_candidate["gap_z"],
            "holding_gap_z": news["gap_z"],
            "reference": news["gap_reference"],
            "interpretation": "ranking proxy only; reasoned two-leg thesis required",
        }
    if age is not None and age >= settings["trading"].get("max_holding_days", 15):
        signals["TIME_ADVISORY"] = {"trading_sessions": age, "automatic_exit": False}
    for kind, evidence in signals.items():
        queue_review(record, kind, evidence, now=now, rearm=True)
    record.trigger_memory["unavailable"] = unavailable
    return signals, unavailable


def monitor_advisories(record, price, settings, *, prior_high=None, now=None):
    now = now or datetime.now(timezone.utc)
    cfg = settings["exits"]["specialized_review"]
    for kind, reference in (
        ("DROP_FROM_PEAK", record.peak_price),
        ("LOSS_FROM_ENTRY", record.entry_price),
    ):
        floor = cfg["drop_pct_threshold"]
        threshold = (
            max(floor, cfg["drop_atr_multiple"] * record.atr14 / reference)
            if cfg["vol_aware"] and record.atr14
            else floor
        )
        drawdown = 1 - price / reference
        memory = record.trigger_memory.setdefault(kind, {"active": False})
        if drawdown < threshold * 0.5:
            memory["active"] = False
        if drawdown >= threshold and not memory["active"]:
            if queue_review(
                record,
                kind,
                {
                    "reference_price": reference,
                    "price": price,
                    "threshold_pct": threshold,
                    "drawdown_pct": drawdown,
                },
                cooldown_hours=cfg["review_cooldown_hours"],
                now=now,
                rearm=True,
            ):
                memory["active"] = True
    if cfg["fifty_two_week_high_review"] and prior_high and price >= prior_high:
        previous = record.trigger_memory.get("last_high_review_price")
        rearm = previous is None or (
            record.atr14
            and price >= previous + record.atr14 * cfg["new_high_rearm_atr_multiple"]
        )
        if rearm and queue_review(
            record,
            "FIFTY_TWO_WEEK_HIGH",
            {"prior_completed_252_high": prior_high, "price": price},
            cooldown_hours=cfg["review_cooldown_hours"],
            now=now,
            rearm=True,
        ):
            record.trigger_memory["last_high_review_price"] = price
