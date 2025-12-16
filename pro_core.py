# ============================================================
# pro_core.py — CORE QUANT & CONFIRMATION ENGINE (FINAL)
# Paket B + Pattern + Trigger compatible
# ============================================================

import time
import requests
import numpy as np
from datetime import datetime

from config import (
    TG_BOT_TOKEN,
    TG_CHAT_ID,
    RSI_CACHE_TTL,
    ORDERBOOK_CACHE_TTL,
    FAPI_BASE,
)

# ============================================================
# TELEGRAM
# ============================================================

def escape_md(text: str) -> str:
    """
    Telegram MarkdownV2 full escape
    """
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, "\\" + c)
    return text


def send_telegram(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": escape_md(text),
                "parse_mode": "MarkdownV2"
            },
            timeout=10
        )
    except Exception:
        pass


# ============================================================
# LOGGING
# ============================================================

def log_signal(event: str, data: dict):
    try:
        entry = {
            "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
        }
        if isinstance(data, dict):
            entry.update(data)
        with open("signals.log", "a", encoding="utf-8") as f:
            f.write(str(entry) + "\n")
    except Exception:
        pass


# ============================================================
# RSI 3m (CACHED)
# ============================================================

_rsi3m_cache = {}

def fetch_rsi_3m_cached(symbol):
    now = time.time()
    c = _rsi3m_cache.get(symbol)
    if c and now - c["ts"] < RSI_CACHE_TTL:
        return c["rsi"], c["trend"]

    rsi = None
    trend = "NEUTRAL"

    try:
        r = requests.get(
            f"{FAPI_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "3m", "limit": 100},
            timeout=8
        ).json()

        closes = [float(k[4]) for k in r]
        if len(closes) >= 15:
            arr = np.array(closes, dtype=float)
            diff = np.diff(arr)

            up = np.where(diff > 0, diff, 0.0)
            dn = np.where(diff < 0, -diff, 0.0)

            gain = np.mean(up[-14:])
            loss = np.mean(dn[-14:]) + 1e-9
            rs = gain / loss
            rsi = round(100 - 100 / (1 + rs), 2)

            if rsi > 52:
                trend = "LONG"
            elif rsi < 48:
                trend = "SHORT"

    except Exception:
        pass

    _rsi3m_cache[symbol] = {"ts": now, "rsi": rsi, "trend": trend}
    return rsi, trend


# ============================================================
# ORDERBOOK IMBALANCE (CACHED)
# ============================================================

_ob_cache = {}

def fetch_orderbook_imbalance_cached(symbol):
    now = time.time()
    c = _ob_cache.get(symbol)
    if c and now - c["ts"] < ORDERBOOK_CACHE_TTL:
        return c["ratio"], c["label"]

    ratio = 1.0
    label = "BALANCED"

    try:
        r = requests.get(
            f"{FAPI_BASE}/fapi/v1/depth",
            params={"symbol": symbol, "limit": 50},
            timeout=5
        ).json()

        bids = r.get("bids", [])
        asks = r.get("asks", [])

        bid = sum(float(p) * float(q) for p, q in bids)
        ask = sum(float(p) * float(q) for p, q in asks)

        if ask > 0:
            ratio = bid / ask

        if ratio >= 3:
            label = f"BID DOM ({ratio:.2f}:1)"
        elif ratio <= 1 / 3:
            label = f"ASK DOM (1:{1/ratio:.2f})"
        else:
            label = f"BALANCED ({ratio:.2f}:1)"

    except Exception:
        pass

    _ob_cache[symbol] = {"ts": now, "ratio": ratio, "label": label}
    return ratio, label


# ============================================================
# SIGNAL QUALITY (0–100)
# ============================================================

def compute_signal_quality(
    *,
    rsi,
    vol_mult,
    oi_chg,
    funding_label,
    price_spike_pct,
    price_pct,
    rsi_3m,
    ob_ratio
):
    score = 0.0

    # RSI stability (15)
    if isinstance(rsi, (int, float)):
        score += max(0.0, 1 - abs(rsi - 50) / 50) * 15

    # Volume spike (20)
    try:
        vm = float(vol_mult)
        score += min(vm / 5.0, 1.0) * 20
    except Exception:
        pass

    # OI change (10)
    try:
        if oi_chg not in [None, "-"]:
            score += min(abs(float(oi_chg)) / 5.0, 1.0) * 10
    except Exception:
        pass

    # Funding bias (5)
    if funding_label not in [None, "-", "PASS"]:
        score += 5

    # Micro spike (20)
    try:
        score += min(abs(float(price_spike_pct)), 1.0) * 20
    except Exception:
        pass

    # 15m move (10)
    try:
        score += min(abs(float(price_pct)) / 5.0, 1.0) * 10
    except Exception:
        pass

    # RSI 3m alignment (10)
    if isinstance(rsi_3m, (int, float)):
        score += max(0.0, 1 - abs(rsi_3m - 50) / 50) * 10

    # Orderbook imbalance (10) — directional strength
    try:
        r = float(ob_ratio)
        if r > 0:
            ratio_norm = max(r, 1.0 / r)
            score += min(max((ratio_norm - 1.0) / 2.0, 0.0), 1.0) * 10
    except Exception:
        pass

    return int(round(score))


# ============================================================
# WAVE CONFIRMATION ENGINE (WCE)
# ============================================================

def compute_wce(
    oi_chg,
    not_chg,
    acc_r,
    pos_r,
    glb_r,
    funding_chg,
    rsi,
    price_pct,
    rsi_3m,
    ob_ratio
):
    try:
        w = {
            "oi": 0.25,
            "not": 0.20,
            "acc": 0.12,
            "pos": 0.08,
            "fund": 0.08,
            "rsi1": 0.10,
            "rsi3": 0.07,
            "ob": 0.05,
        }

        dirf = 1.0 if price_pct >= 0 else -1.0

        def _num(x, default=0.0):
            try:
                return float(x)
            except Exception:
                return default

        raw = 0.0
        raw += w["oi"]   * (_num(oi_chg) / 100.0) * dirf
        raw += w["not"]  * (_num(not_chg) / 100.0) * dirf
        raw += w["acc"]  * ((_num(acc_r, 50) - 50) / 50.0) * dirf
        raw += w["pos"]  * ((_num(pos_r, 50) - 50) / 50.0) * dirf
        raw += w["fund"] * (-_num(funding_chg) / 100.0)

        if isinstance(rsi, (int, float)):
            raw += w["rsi1"] * ((1 - abs(rsi - 50) / 50) * 2 - 1)

        if isinstance(rsi_3m, (int, float)):
            raw += w["rsi3"] * ((1 - abs(rsi_3m - 50) / 50) * 2 - 1)

        try:
            r = float(ob_ratio)
            if r > 0:
                ratio_norm = max(r, 1.0 / r)
                raw += w["ob"] * min(max((ratio_norm - 1.0) / 2.0, 0.0), 1.0) * dirf
        except Exception:
            pass

        raw = max(min(raw, 1.0), -1.0)
        score = int(round((raw + 1) / 2 * 100))

        if score >= 55:
            trend = "LONG" if price_pct > 0 else "SHORT"
        else:
            trend = "UNCERTAIN"

        fake = "HIGH" if score < 40 else "MEDIUM" if score < 60 else "LOW"
        conf = "HIGH" if score >= 80 else "MEDIUM" if score >= 60 else "LOW"

        text = (
            "\n🔥 Wave Confirmation\n"
            f"Score: {score}%\n"
            f"Trend: {trend}\n"
            f"Fake-out risk: {fake}\n"
            f"Confidence: {conf}"
        )
        return score, trend, fake, conf, text

    except Exception:
        return 50, "UNKNOWN", "MEDIUM", "LOW", "\n🔥 Wave Confirmation: unavailable"
