# ============================================================
# trigger_engine.py — FINAL TRIGGER ENGINE (Paket B base + Pattern)
# ============================================================

import time
import requests
import numpy as np

from config import (
    FAPI_BASE,
    LOOKBACK_MIN,
    SHORT_WINDOW,
    RSI_PERIOD,
    MIN_RECENT_VOLUME_USDT,

    START_PCT,
    START_VOLUME_SPIKE,
    START_MICRO_PCT,
    FAKE_VOLUME_STRENGTH,
    FAKE_RECENT_MIN_USDT,
    FAKE_RECENT_STRONG_USDT,

    STEP_PCT,
    STEP_VOLUME_SPIKE,
    STEP_VOLUME_STRENGTH,

    MIN24H,
)

from pro_core import (
    send_telegram,
    log_signal,
    get_24h_volume_cached,
    fetch_rsi_3m_cached,
    fetch_orderbook_imbalance_cached,
    compute_signal_quality,
    compute_wce,
)

from sentiment_engine import fetch_sentiment_cached
from exit_reverse_engine import maybe_exit_reverse
from pattern_engine import generate_pattern_analysis


# ============================================================

def get_closes(symbol, limit=100, interval="1m"):
    try:
        r = requests.get(
            f"{FAPI_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=8
        ).json()
        return [float(k[4]) for k in r] if isinstance(r, list) else []
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
        loss = np.mean(dn[:period]) + 1e-9
        for i in range(period, len(diff)):
            gain = (gain * (period - 1) + up[i]) / period
            loss = (loss * (period - 1) + dn[i]) / period
        rs = gain / loss
        return round(100 - 100 / (1 + rs), 2)
    except Exception:
        return None


def classify_impulse_stage(vol_mult, volume_strength):
    try:
        vol_mult = float(vol_mult or 1.0)
        volume_strength = float(volume_strength or 0.0)
    except Exception:
        return "UNKNOWN"

    if (vol_mult < 4.0) and (volume_strength < 2.0):
        return "EARLY"
    if (vol_mult < 6.0) and (volume_strength < 2.5):
        return "MID"
    return "LATE"


def score_level(score):
    return "WEAK" if score < 40 else "GOOD" if score < 70 else "STRONG" if score < 90 else "ULTRA"


def _coerce_sentiment(metrics_or_tuple):
    """
    Normalizes sentiment_engine output.
    Accepts either:
      - dict metrics
      - (text, dict metrics)
    Returns: (sentiment_text, metrics_dict)
    """
    if isinstance(metrics_or_tuple, tuple) and len(metrics_or_tuple) == 2:
        text, metrics = metrics_or_tuple
        if isinstance(metrics, dict):
            return (text or ""), metrics
    if isinstance(metrics_or_tuple, dict):
        return "", metrics_or_tuple
    return "", {
        "oi_chg": "-", "not_chg": "-", "acc_r": "-", "pos_r": "-", "glb_r": "-",
        "last_f": "-", "funding_change": 0.0
    }


# ============================================================

class TriggerEngine:
    def __init__(self, tracked_syms):
        self.tracked_syms = set(tracked_syms or [])
        self.state = {}
        self.last_seen = {}
        self.last_any_msg_ts = 0.0

    def handle_miniticker(self, msg):
        if isinstance(msg, list):
            for m in msg:
                if isinstance(m, dict):
                    self._process(m)
        elif isinstance(msg, dict):
            self._process(msg)

    def _process(self, msg):
        symbol = msg.get("s") or msg.get("symbol")
        if not symbol or not symbol.endswith("USDT"):
            return
        if self.tracked_syms and symbol not in self.tracked_syms:
            return

        now = time.time()
        self.last_any_msg_ts = now

        # -------------------------
        # PRICE
        # -------------------------
        try:
            price = float(msg.get("c", msg.get("lastPrice", 0.0)))
        except Exception:
            return
        if price <= 0:
            return

        # -------------------------
        # VOLUME (USDT preferred)
        # -------------------------
        # Prefer quote volume if present (q)
        try:
            vol_total = float(msg.get("q", msg.get("quoteVolume", msg.get("v", msg.get("volume", 0.0)))))
        except Exception:
            vol_total = 0.0

        e = self.state.setdefault(symbol, {
            "prices": [],
            "vols": [],
            "last_v": None,          # last cumulative volume (USDT)
            "tracking": False,
            "phase": "IDLE",

            "start_price": None,
            "last_step_price": None,
            "last_step_time": None,

            "start_time": None,
            "direction": None,

            "last_wce": None,
            "last_rsi3m_trend": None,
        })

        # append price
        e["prices"].append(price)

        # cumulative -> delta
        if e["last_v"] is None:
            diff_vol = 0.0
        else:
            diff_vol = max(vol_total - e["last_v"], 0.0)
        e["last_v"] = vol_total
        e["vols"].append(diff_vol)

        # cap history
        if len(e["prices"]) > 1800:
            e["prices"] = e["prices"][-1800:]
            e["vols"] = e["vols"][-1800:]

        self.last_seen[symbol] = now

        if len(e["prices"]) < 5:
            return

        # -------------------------
        # 15m change
        # -------------------------
        lb = LOOKBACK_MIN * 60
        base = e["prices"][-lb] if len(e["prices"]) >= lb else e["prices"][0]
        pct_15m = ((price - base) / base * 100.0) if base else 0.0

        # -------------------------
        # Volume spike (1m vs prev 5m)
        # -------------------------
        vols = e["vols"]
        recent_1m = sum(vols[-60:]) if len(vols) >= 60 else 0.0
        prev_5m = sum(vols[-360:-60]) if len(vols) >= 360 else 0.0
        baseline = (prev_5m / 5.0) if prev_5m > 0 else 0.0
        vol_mult = (recent_1m / baseline) if baseline > 0 else 1.0

        # -------------------------
        # Micro spike (short window)
        # -------------------------
        short_pct = 0.0
        if len(e["prices"]) >= SHORT_WINDOW:
            b = e["prices"][-SHORT_WINDOW]
            short_pct = ((price - b) / b * 100.0) if b else 0.0

        # -------------------------
        # Volume strength (15m vs prev 15m)
        # -------------------------
        last15 = sum(vols[-900:]) if len(vols) >= 900 else 0.0
        prev15 = sum(vols[-1800:-900]) if len(vols) >= 1800 else 0.0
        volume_strength = (last15 / prev15) if prev15 > 0 else 0.0

        stage = classify_impulse_stage(vol_mult, volume_strength)

        # ============================================================
        # START (PASSIVE MODE)
        # ============================================================
        if (not e["tracking"]) and e["phase"] in ("IDLE", "EXITED"):
            vol24 = get_24h_volume_cached(symbol)

            if (
                abs(pct_15m) >= START_PCT
                and vol_mult >= START_VOLUME_SPIKE
                and abs(short_pct) >= START_MICRO_PCT

                and volume_strength >= FAKE_VOLUME_STRENGTH
                and recent_1m >= FAKE_RECENT_MIN_USDT
                and (vol_mult <= 50 or recent_1m >= FAKE_RECENT_STRONG_USDT)

                and vol24 >= MIN24H
                and recent_1m >= MIN_RECENT_VOLUME_USDT
            ):
                closes = get_closes(symbol, limit=100, interval="1m")
                rsi = compute_rsi(closes, RSI_PERIOD)

                sentiment_raw = fetch_sentiment_cached(symbol)
                sentiment_text, metrics = _coerce_sentiment(sentiment_raw)

                rsi3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
                ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)

                signal_q = compute_signal_quality(
                    rsi=rsi,
                    vol_mult=vol_mult,
                    oi_chg=metrics.get("oi_chg"),
                    funding_label=metrics.get("last_f"),
                    price_spike_pct=short_pct,
                    price_pct=pct_15m,
                    rsi_3m=rsi3m,
                    ob_ratio=ob_ratio
                )

                wce_score, wce_trend, wce_fake, wce_conf, wce_text = compute_wce(
                    metrics.get("oi_chg"),
                    metrics.get("not_chg"),
                    metrics.get("acc_r"),
                    metrics.get("pos_r"),
                    metrics.get("glb_r"),
                    metrics.get("funding_change"),
                    rsi,
                    pct_15m,
                    rsi_3m=rsi3m,
                    ob_ratio=ob_ratio
                )

                # Pattern (guarded)
                try:
                    pattern = generate_pattern_analysis(
                        metrics,
                        pct_15m,
                        rsi,
                        rsi3m,
                        ob_ratio,
                        ob_label,
                        stage,
                        "full"
                    )
                except Exception:
                    pattern = ""

                text = (
                    "🚀 START\n"
                    f"{symbol}\n\n"
                    f"Change 15m: {pct_15m:+.2f}%\n"
                    f"Vol spike: x{vol_mult:.2f}\n"
                    f"Vol strength: {volume_strength:.2f}\n"
                    f"Micro: {short_pct:+.2f}%\n"
                    f"Stage: {stage}\n"
                    f"{pattern}\n"
                    f"SignalQ: {signal_q} ({score_level(signal_q)})\n\n"
                    f"{wce_text}\n"
                    f"{sentiment_text}"
                )

                send_telegram(text)

                e.update({
                    "tracking": True,
                    "phase": "STARTED",

                    "start_price": price,
                    "last_step_price": price,
                    "last_step_time": now,

                    "start_time": now,
                    "direction": "LONG" if pct_15m > 0 else "SHORT",

                    "last_wce": wce_score,
                    "last_rsi3m_trend": rsi3m_trend,
                })

                log_signal("START", {
                    "symbol": symbol,
                    "pct_15m": pct_15m,
                    "vol_mult": vol_mult,
                    "volume_strength": volume_strength,
                    "short_pct": short_pct,
                    "signal_q": signal_q,
                    "wce_score": wce_score,
                    "stage": stage,
                })

        # ============================================================
        # TRACKING MODE (STEP + EXIT/REVERSE)
        # ============================================================
        if e["tracking"]:
            # STEP
            last_step_price = e.get("last_step_price") or price
            pct_from_last_step = ((price - last_step_price) / last_step_price * 100.0) if last_step_price else 0.0

            step_fired = False
            if (
                abs(pct_from_last_step) >= STEP_PCT
                and vol_mult >= STEP_VOLUME_SPIKE
                and volume_strength >= STEP_VOLUME_STRENGTH
            ):
                closes = get_closes(symbol, limit=100, interval="1m")
                rsi = compute_rsi(closes, RSI_PERIOD)

                sentiment_raw = fetch_sentiment_cached(symbol)
                sentiment_text, metrics = _coerce_sentiment(sentiment_raw)

                rsi3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
                ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)

                signal_q = compute_signal_quality(
                    rsi=rsi,
                    vol_mult=vol_mult,
                    oi_chg=metrics.get("oi_chg"),
                    funding_label=metrics.get("last_f"),
                    price_spike_pct=short_pct,
                    price_pct=pct_15m,
                    rsi_3m=rsi3m,
                    ob_ratio=ob_ratio
                )

                wce_score, wce_trend, wce_fake, wce_conf, wce_text = compute_wce(
                    metrics.get("oi_chg"),
                    metrics.get("not_chg"),
                    metrics.get("acc_r"),
                    metrics.get("pos_r"),
                    metrics.get("glb_r"),
                    metrics.get("funding_change"),
                    rsi,
                    pct_15m,
                    rsi_3m=rsi3m,
                    ob_ratio=ob_ratio
                )

                # Pattern short (guarded)
                try:
                    pattern = generate_pattern_analysis(
                        metrics,
                        pct_15m,
                        rsi,
                        rsi3m,
                        ob_ratio,
                        ob_label,
                        stage,
                        "short"
                    )
                except Exception:
                    pattern = ""

                text = (
                    "⚡ STEP\n"
                    f"{symbol}\n\n"
                    f"Change 15m: {pct_15m:+.2f}%\n"
                    f"Δ from last step: {pct_from_last_step:+.2f}%\n"
                    f"Vol spike: x{vol_mult:.2f}\n"
                    f"Vol strength: {volume_strength:.2f}\n"
                    f"Micro: {short_pct:+.2f}%\n"
                    f"Stage: {stage}\n"
                    f"{pattern}\n"
                    f"SignalQ: {signal_q} ({score_level(signal_q)})\n\n"
                    f"{wce_text}\n"
                    f"{sentiment_text}"
                )

                send_telegram(text)

                e["last_step_price"] = price
                e["last_step_time"] = now
                e["phase"] = "ACTIVE"
                e["last_wce"] = wce_score
                e["last_rsi3m_trend"] = rsi3m_trend

                log_signal("STEP", {
                    "symbol": symbol,
                    "pct_15m": pct_15m,
                    "pct_from_last_step": pct_from_last_step,
                    "vol_mult": vol_mult,
                    "volume_strength": volume_strength,
                    "short_pct": short_pct,
                    "signal_q": signal_q,
                    "wce_score": wce_score,
                    "stage": stage,
                })

                step_fired = True

            # EXIT / REVERSE (even if step fired, it's ok to skip this tick)
            if not step_fired:
                maybe_exit_reverse(
                    symbol,
                    e,
                    price,
                    pct_15m,
                    vol_mult,
                    volume_strength,
                    short_pct,
                    recent_1m,
                    baseline,
                    now
                )
