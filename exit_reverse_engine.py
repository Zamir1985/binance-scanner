# ============================================================
# exit_reverse_engine.py — PRO Exit & Reverse (Aligned to Package B logic)
# ============================================================

import time
import requests
import numpy as np

from config import FAPI_BASE

from sentiment_engine import fetch_sentiment_cached
from pro_core import (
    send_telegram,
    log_signal,
    fetch_rsi_3m_cached,
    fetch_orderbook_imbalance_cached,
    compute_wce,
    compute_signal_quality,
)

from pattern_engine import generate_pattern_analysis


# -------------------------
# SAFE DEFAULTS (fallback)
# -------------------------
try:
    from config import (
        EXIT_ENABLED,
        EXIT_WCE_DROP,
        EXIT_VOLUME_DROP,
        EXIT_MIN_AGE,
        EXIT_CHECK_INTERVAL,
        EXIT_USE_RSI3M_FLIP,
        EXIT_MICRO_REVERSE,
        REVERSE_ENABLED,
        REVERSE_WCE_MIN,
        REVERSE_SIGNALQ_MIN,
    )
except Exception:
    EXIT_ENABLED = True
    EXIT_WCE_DROP = 25.0
    EXIT_VOLUME_DROP = 0.55
    EXIT_MIN_AGE = 240
    EXIT_CHECK_INTERVAL = 20
    EXIT_USE_RSI3M_FLIP = True
    EXIT_MICRO_REVERSE = 0.15

    REVERSE_ENABLED = True
    REVERSE_WCE_MIN = 60.0
    REVERSE_SIGNALQ_MIN = 60.0


# -------------------------
# Local utils (no utils.py dependency)
# -------------------------
def get_closes(symbol, limit=100, interval="1m"):
    try:
        r = requests.get(
            f"{FAPI_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=8
        ).json()
        if isinstance(r, list):
            return [float(k[4]) for k in r]
        return []
    except Exception:
        return []


def compute_rsi(prices, period=14):
    try:
        if len(prices) <= period:
            return None
        arr = np.array(prices, dtype=float)
        diff = np.diff(arr)
        up = np.where(diff > 0, diff, 0.0)
        dn = np.where(diff < 0, -diff, 0.0)

        gain = np.mean(up[:period])
        loss = np.mean(dn[:period])

        for i in range(period, len(diff)):
            gain = (gain * (period - 1) + up[i]) / period
            loss = (loss * (period - 1) + dn[i]) / period

        rs = gain / (loss + 1e-10)
        return round(100 - 100 / (1 + rs), 2)
    except Exception:
        return None


def classify_impulse_stage(vol_mult, volume_strength):
    try:
        vm = float(vol_mult or 1.0)
        vs = float(volume_strength or 0.0)
    except Exception:
        return "UNKNOWN"

    if (vm < 4.0) and (vs < 2.0):
        return "EARLY"
    if (vm < 6.0) and (vs < 2.5):
        return "MID"
    return "LATE"


def _score_level(score: int) -> str:
    if score < 40:
        return "WEAK ⚠️"
    if score < 70:
        return "GOOD 👍"
    if score < 90:
        return "STRONG 🔥"
    return "ULTRA 🚀"


# ============================================================
# MAIN: EXIT + REVERSE (Package B style)
# ============================================================
def maybe_exit_reverse(
    symbol,
    entry,
    price,
    pct_15m,
    vol_mult,
    volume_strength,
    short_pct,
    recent_1m,
    baseline_avg_1m,
    now_ts
):
    """
    FINAL adapter expected by trigger_engine.py
    Implements EXIT + optional REVERSE (Package B logic).
    """

    if not EXIT_ENABLED:
        return

    if not entry.get("tracking"):
        return

    if entry.get("phase") not in ("ACTIVE", "STARTED", "START"):
        # allow slight naming drift, but still protect
        # if your engine uses "ACTIVE" it's fine
        pass

    start_time = entry.get("start_time")
    if not start_time:
        return

    if now_ts - start_time < EXIT_MIN_AGE:
        return

    # throttle checks
    last_check = entry.get("last_exit_check", 0.0)
    if now_ts - last_check < EXIT_CHECK_INTERVAL:
        return
    entry["last_exit_check"] = now_ts

    # volume collapse
    vol_drop = 0.0
    try:
        if baseline_avg_1m and baseline_avg_1m > 0:
            vol_drop = max(0.0, 1.0 - (recent_1m / baseline_avg_1m))
    except Exception:
        vol_drop = 0.0

    # indicators
    closes = get_closes(symbol, limit=100, interval="1m")
    rsi = compute_rsi(closes, 14)

    metrics = fetch_sentiment_cached(symbol)
    rsi3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
    ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)
    stage_label = classify_impulse_stage(vol_mult, volume_strength)

    # Signal Quality (pro_core signature)
    signal_q = compute_signal_quality(
        rsi=rsi,
        vol_mult=vol_mult,
        oi_chg=metrics.get("oi_chg"),
        funding=metrics.get("last_f"),
        price_spike_pct=short_pct,
        price_pct=pct_15m,
        rsi_3m=rsi3m,
        ob_ratio=ob_ratio
    )

    # WCE (pro_core signature expects 10 args)
    wce_score, wce_trend, wce_fake, wce_conf, wce_text = compute_wce(
        metrics.get("oi_chg") or 0.0,
        metrics.get("not_chg") or 0.0,
        metrics.get("acc_r") or 50.0,
        metrics.get("pos_r") or 50.0,
        metrics.get("glb_r") or 50.0,
        metrics.get("funding_change") or 0.0,
        rsi,
        pct_15m,
        rsi3m,
        ob_ratio
    )

    prev_wce = entry.get("last_wce", wce_score)
    prev_rsi3m_trend = entry.get("last_rsi3m_trend")
    direction = entry.get("direction", "UNKNOWN")

    reasons = []

    # 1) WCE drop
    try:
        wce_drop = float(prev_wce) - float(wce_score)
    except Exception:
        wce_drop = 0.0

    if wce_drop >= EXIT_WCE_DROP:
        reasons.append(f"WCE drop {prev_wce:.0f} → {wce_score:.0f}")

    # 2) Volume collapse
    if vol_drop >= EXIT_VOLUME_DROP:
        reasons.append(f"Volume collapse {vol_drop*100:.1f}%")

    # 3) Micro reverse vs direction
    try:
        sp = float(short_pct or 0.0)
    except Exception:
        sp = 0.0

    if direction == "LONG" and sp <= -EXIT_MICRO_REVERSE:
        reasons.append(f"Micro reverse {sp:+.2f}% vs LONG")
    elif direction == "SHORT" and sp >= EXIT_MICRO_REVERSE:
        reasons.append(f"Micro reverse {sp:+.2f}% vs SHORT")

    # 4) RSI3m flip
    if EXIT_USE_RSI3M_FLIP and prev_rsi3m_trend and rsi3m_trend and rsi3m_trend != prev_rsi3m_trend:
        reasons.append(f"RSI(3m) flip {prev_rsi3m_trend} → {rsi3m_trend}")

    # need at least 2 reasons (Package B strictness)
    if len(reasons) < 2:
        entry["last_wce"] = wce_score
        entry["last_trend_dir"] = wce_trend
        entry["last_rsi3m_trend"] = rsi3m_trend
        entry["last_signal_q"] = signal_q
        return

    # anti-spam exit
    last_exit = entry.get("exit_sent_at")
    if last_exit and (now_ts - last_exit) < 60:
        entry["last_wce"] = wce_score
        entry["last_trend_dir"] = wce_trend
        entry["last_rsi3m_trend"] = rsi3m_trend
        entry["last_signal_q"] = signal_q
        return

    # Pattern (exit mode)
    pattern_block = generate_pattern_analysis(
        metrics=metrics,
        price_pct=pct_15m,
        rsi=rsi,
        rsi_3m=rsi3m,
        ob_ratio=ob_ratio,
        ob_label=ob_label,
        stage_label=stage_label,
        mode="exit"
    )

    # REVERSE evaluation (must be here; trigger_engine does not evaluate)
    reverse_note = "🔄 Reverse opportunity: NONE"
    reverse_triggered = False

    if REVERSE_ENABLED and direction in ("LONG", "SHORT"):
        reverse_ok = False

        if (
            direction == "LONG"
            and isinstance(wce_trend, str) and wce_trend.startswith("SHORT")
            and wce_score >= REVERSE_WCE_MIN
            and signal_q >= REVERSE_SIGNALQ_MIN
        ):
            reverse_ok = True

        if (
            direction == "SHORT"
            and isinstance(wce_trend, str) and wce_trend.startswith("LONG")
            and wce_score >= REVERSE_WCE_MIN
            and signal_q >= REVERSE_SIGNALQ_MIN
        ):
            reverse_ok = True

        if reverse_ok:
            reverse_note = "🔄 Reverse opportunity: STRONG (consider opposite wave)"
            reverse_triggered = True
        else:
            reverse_note = "🔄 Reverse opportunity: WEAK / WAIT"

    reasons_text = "\n".join(f"• {r}" for r in reasons)

    caption = (
        "⚠ EXIT SIGNAL\n"
        f"{symbol}\n\n"
        f"Trend: {direction} → {wce_trend}\n"
        f"Price(15m): {pct_15m:+.2f}%\n"
        f"Stage: {stage_label}\n"
        f"Volume drop(1m vs 5m): {vol_drop*100:.1f}%\n"
        f"Micro spike (short): {sp:+.2f}%\n"
        f"RSI(1m): {rsi}\n"
        f"RSI(3m): {rsi3m} ({rsi3m_trend})\n"
        f"Orderbook: {ob_label}\n"
        f"Signal Quality: {signal_q}/100 ({_score_level(signal_q)})\n"
        f"WCE Score: {wce_score}%\n\n"
        f"Reasons:\n{reasons_text}\n\n"
        f"{reverse_note}\n"
        f"{wce_text}\n"
        f"{pattern_block}"
    )

    send_telegram(caption)

    # update entry state
    entry["exit_sent_at"] = now_ts
    entry["tracking"] = False
    entry["phase"] = "EXITED"
    entry["stopped_at"] = now_ts

    # logs
    log_signal("EXIT", {
        "symbol": symbol,
        "direction": direction,
        "pct_15m": pct_15m,
        "short_pct": sp,
        "vol_drop": vol_drop,
        "wce_prev": prev_wce,
        "wce_now": wce_score,
        "signal_q": signal_q,
        "reasons": reasons,
        "reverse_ok": reverse_triggered,
    })

    if reverse_triggered:
        log_signal("REVERSE", {
            "symbol": symbol,
            "direction": direction,
            "new_trend": wce_trend,
            "wce_score": wce_score,
            "signal_q": signal_q
        })

    entry["last_wce"] = wce_score
    entry["last_trend_dir"] = wce_trend
    entry["last_rsi3m_trend"] = rsi3m_trend
    entry["last_signal_q"] = signal_q
