# ============================================================
# sentiment_engine.py — REAL Binance Futures Sentiment (FINAL)
# Trigger / Pattern / ProCore compatible
# ============================================================

import time
import requests
from config import FAPI_BASE

_SENTIMENT_CACHE = {}
_SENTIMENT_TTL = 30


def _funding_interval_label(v: float) -> str:
    """
    Maps funding rate to discrete interval label (string).
    """
    try:
        v = round(v, 4)
    except Exception:
        return "PASS"

    intervals = [-2, -1, -0.5, -0.1, 0, 0.1, 0.5, 1, 2]
    if v < intervals[0]:
        return "< -2"
    if v >= intervals[-1]:
        return "> 2"
    for i in range(len(intervals) - 1):
        if intervals[i] <= v < intervals[i + 1]:
            return f"{intervals[i]} - {intervals[i+1]}"
    return "PASS"


def fetch_sentiment_cached(symbol: str) -> dict:
    """
    Returns ONLY metrics dict.
    Keys:
      oi_chg, not_chg, acc_r, pos_r, glb_r, last_f, funding_change
    """
    now = time.time()
    cached = _SENTIMENT_CACHE.get(symbol)

    if cached and now - cached["ts"] < _SENTIMENT_TTL:
        return cached["metrics"]

    metrics = {
        "oi_chg": "-",
        "not_chg": "-",
        "acc_r": "-",
        "pos_r": "-",
        "glb_r": "-",
        "last_f": "PASS",
        "funding_change": 0.0,
    }

    try:
        base = f"{FAPI_BASE}/futures/data"

        # -------- OI + Notional (15m) --------
        oi = requests.get(
            f"{base}/openInterestHist",
            params={"symbol": symbol, "period": "15m", "limit": 2},
            timeout=6
        ).json()

        if isinstance(oi, list) and len(oi) >= 2:
            prev_oi = float(oi[0].get("sumOpenInterest", 0))
            curr_oi = float(oi[-1].get("sumOpenInterest", 0))
            prev_not = float(oi[0].get("sumOpenInterestValue", 0))
            curr_not = float(oi[-1].get("sumOpenInterestValue", 0))

            if prev_oi > 0:
                metrics["oi_chg"] = (curr_oi - prev_oi) / prev_oi * 100
            if prev_not > 0:
                metrics["not_chg"] = (curr_not - prev_not) / prev_not * 100

        # -------- Long / Short Ratios --------
        def ratio(endpoint):
            try:
                r = requests.get(endpoint, timeout=6).json()
                if isinstance(r, list) and r:
                    return float(r[-1].get("longShortRatio", 0))
            except Exception:
                pass
            return "-"

        metrics["acc_r"] = ratio(f"{base}/topLongShortAccountRatio?symbol={symbol}&period=15m&limit=1")
        metrics["pos_r"] = ratio(f"{base}/topLongShortPositionRatio?symbol={symbol}&period=15m&limit=1")
        metrics["glb_r"] = ratio(f"{base}/globalLongShortAccountRatio?symbol={symbol}&period=15m&limit=1")

        # -------- Funding --------
        fund = requests.get(
            f"{base}/fundingRate",
            params={"symbol": symbol, "limit": 2},
            timeout=6
        ).json()

        if isinstance(fund, list) and len(fund) >= 2:
            f_now = float(fund[-1].get("fundingRate", 0))
            f_prev = float(fund[-2].get("fundingRate", 0))
            metrics["funding_change"] = (f_now - f_prev) * 100
            metrics["last_f"] = _funding_interval_label(f_now)

    except Exception:
        pass

    _SENTIMENT_CACHE[symbol] = {"ts": now, "metrics": metrics}
    return metrics

# ============================================================
# SENTIMENT TEXT FORMATTER
# ============================================================

def format_sentiment_text(metrics: dict) -> str:
    """
    Formats sentiment metrics into human-readable text block
    """
    if not metrics:
        return ""

    lines = ["\n🧠 SENTIMENT"]

    if metrics.get("oi_chg") is not None:
        lines.append(f"OI change: {metrics['oi_chg']:+.2f}%")

    if metrics.get("not_chg") is not None:
        lines.append(f"Notional change: {metrics['not_chg']:+.2f}%")

    if metrics.get("acc_r") is not None:
        lines.append(f"Retail L/S: {metrics['acc_r']:.2f}")

    if metrics.get("pos_r") is not None:
        lines.append(f"Pro L/S: {metrics['pos_r']:.2f}")

    if metrics.get("glb_r") is not None:
        lines.append(f"Global L/S: {metrics['glb_r']:.2f}")

    if metrics.get("last_f") not in [None, "PASS"]:
        lines.append(f"Funding: {metrics['last_f']}")

    if metrics.get("funding_change"):
        lines.append(f"Funding Δ: {metrics['funding_change']:+.2f}%")

    return "\n".join(lines)
