# ============================================================
#   Binance Futures Scanner — Railway Version (BALANCED PRO)
#   LOGIC/CALC: PRESERVED
#   PERF: NO WS BLOCKING (START/EXIT via queue workers)
# ============================================================

import os
import time
import json
import threading
import requests
import numpy as np
from datetime import datetime

from binance import ThreadedWebsocketManager
from binance.client import Client

import functools
print = functools.partial(print, flush=True)

from queue import Queue, Full, Empty
from concurrent.futures import ThreadPoolExecutor

# ============================================================
# GLOBAL STATES
# ============================================================

state = {}             # per-symbol signal tracking
last_seen = {}         # symbol timestamp monitor
tracked_syms = set()

# API caches
sentiment_cache = {}
rsi3m_cache = {}
orderbook_cache = {}
vol24_cache = {}

# Uptime / monitor states
START_TIME = time.time()
last_any_msg_ts = 0.0
last_heartbeat_ts = 0.0

# Websocket manager holder
ws_manager = None

# Task queues
task_queue = Queue(maxsize=2000)       # START/EXIT heavy work
telegram_queue = Queue(maxsize=2000)   # Telegram messages

# Workers config
ANALYSIS_WORKERS = int(os.getenv("ANALYSIS_WORKERS", "4"))
TELEGRAM_WORKERS = int(os.getenv("TELEGRAM_WORKERS", "1"))

# Optional: REST pool (kept; not required for logic)
REST_POOL_SIZE = int(os.getenv("REST_POOL_SIZE", "6"))
rest_pool = ThreadPoolExecutor(max_workers=REST_POOL_SIZE)

# ============================================================
# CONFIG
# ============================================================

START_PCT = 5.0
START_VOLUME_SPIKE = 3.0
START_MICRO_PCT = 0.05

FAKE_VOLUME_STRENGTH = 1.5
FAKE_RECENT_MIN_USDT = 2000
FAKE_RECENT_STRONG_USDT = 10000

MOMENTUM_THRESHOLD = START_PCT
PATTERN_PCT = 3.0

MIN24H = 2_000_000

STEP_PCT = 5.0
STEP_VOLUME_SPIKE = 2.0
STEP_VOLUME_STRENGTH = 1.2

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

LOOKBACK_MIN = 15
RSI_PERIOD = 14
TOP_N = 50
SHORT_WINDOW = 5

RSI3M_CACHE_TTL = 20
ORDERBOOK_CACHE_TTL = 10
SENTIMENT_CACHE_TTL = 30
VOL24_CACHE_TTL = 300

HEARTBEAT_ENABLED = True
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "1800"))

LOG_ENABLED = True
LOG_FILE = os.getenv("SIGNAL_LOG_FILE", "signals.log")

WATCHDOG_ENABLED = True
WATCHDOG_NO_MSG_TIMEOUT = int(os.getenv("WATCHDOG_NO_MSG_TIMEOUT", "900"))
WATCHDOG_MIN_UPTIME = 300

# ============================================================
# SPOT MODEL — Signal Quality Adjustment (backend only)
# ============================================================

SPOT_MODEL_ENABLED = True

SPOT_ADJ_MAX_POS = 15
SPOT_ADJ_MAX_NEG = -20

SPOT_LATE_STAGE_PENALTY = -8
SPOT_OI_UP_NOTIONAL_DOWN_PENALTY = -15
SPOT_RETAIL_CROWD_PENALTY = -7
SPOT_HEALTHY_CONT_BONUS = +10
SPOT_SHORT_SQUEEZE_BONUS = +8

# ============================================================
# API KEYS
# ============================================================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
FAPI = "https://fapi.binance.com"

# ============================================================
# HTTP SESSION (connection pooling) — does NOT change logic
# ============================================================

_http = requests.Session()
_http.headers.update({"User-Agent": "balanced-pro-scanner/1.0"})
# keep default adapters; Railway usually fine

# ============================================================
# MARKDOWN ESCAPE (Telegram MarkdownV2)
# ============================================================

def escape_md(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, "\\" + c)
    return text

# ============================================================
# LOGGING
# ============================================================

def log_signal(event_type, data: dict):
    if not LOG_ENABLED:
        return
    try:
        entry = {
            "ts": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event_type
        }
        entry.update(data or {})
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print("log_signal error:", e)

# ============================================================
# UTILITIES (24h Volume / Klines / RSI)
# ============================================================

def get_24h_volume(symbol):
    try:
        r = _http.get(f"{FAPI}/fapi/v1/ticker/24hr", params={"symbol": symbol}, timeout=8)
        return float(r.json().get("quoteVolume", 0))
    except Exception:
        return 0.0

def get_24h_volume_cached(symbol):
    now = time.time()
    e = vol24_cache.get(symbol)
    if e and now - e["ts"] < VOL24_CACHE_TTL:
        return e["value"]
    v = get_24h_volume(symbol)
    vol24_cache[symbol] = {"ts": now, "value": v}
    return v

def get_24h_volume_cache_only(symbol):
    """
    WS thread üçün təhlükəsiz variant.
    Heç vaxt REST çağırmaz.
    Cache yoxdursa və ya köhnədirsə 0 qaytarır.
    """
    e = vol24_cache.get(symbol)
    if not e:
        return 0.0

    if time.time() - e.get("ts", 0) > VOL24_CACHE_TTL:
        return 0.0

    return float(e.get("value", 0.0))

def get_closes(symbol, limit=100, interval="1m"):
    try:
        r = _http.get(
            f"{FAPI}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=8
        )
        data = r.json()
        if isinstance(data, list):
            return [float(k[4]) for k in data]
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
            gain = (gain*(period-1) + up[i]) / period
            loss = (loss*(period-1) + dn[i]) / period

        rs = gain / (loss + 1e-10)
        return round(100 - 100/(1+rs), 2)
    except Exception:
        return None

def rsi_momentum_component(rsi):
    """
    Momentum-aligned RSI scoring.
    Trend strategiyası üçün optimallaşdırılıb.
    """
    if rsi is None:
        return 0.0

    if 55 <= rsi <= 75:
        return 1.0          # ideal momentum
    if 75 < rsi <= 85:
        return 0.7          # late but strong
    if 45 <= rsi < 55:
        return 0.4          # neutral / early
    if 35 <= rsi < 45:
        return 0.2          # weak
    return 0.0              # extreme / bad

def rsi_3m_trend_component(rsi3):
    """
    Higher timeframe RSI trend confirmation.
    Continuation / trend alignment üçün.
    """
    if rsi3 is None:
        return 0.0

    if 52 <= rsi3 <= 70:
        return 1.0
    if 48 <= rsi3 < 52:
        return 0.5
    return 0.0

# ============================================================
# VOL24 CACHE WARMUP (NON-WS)
# ============================================================

def warmup_vol24(symbols):
    """
    WS-dən kənarda 24h volume cache doldurur.
    REST burda icazəlidir.
    """
    for s in symbols:
        try:
            get_24h_volume_cached(s)
        except Exception:
            pass

# ============================================================
# RSI 3m + ORDERBOOK + IMPULSE STAGE
# ============================================================

def fetch_rsi_3m_cached(symbol, ttl=RSI3M_CACHE_TTL):
    now = time.time()
    entry = rsi3m_cache.get(symbol)
    if entry and now - entry["ts"] < ttl:
        return entry["rsi"], entry["trend"]

    closes = get_closes(symbol, limit=100, interval="3m")
    rsi3 = compute_rsi(closes, period=14)

    if rsi3 is None:
        trend = "NEUTRAL"
    else:
        if rsi3 > 52:
            trend = "LONG"
        elif rsi3 < 48:
            trend = "SHORT"
        else:
            trend = "NEUTRAL"

    rsi3m_cache[symbol] = {"ts": now, "rsi": rsi3, "trend": trend}
    return rsi3, trend

def fetch_orderbook_imbalance_cached(symbol, ttl=ORDERBOOK_CACHE_TTL):
    now = time.time()
    entry = orderbook_cache.get(symbol)
    if entry and now - entry["ts"] < ttl:
        return entry["ratio"], entry["label"]

    try:
        r = _http.get(
            f"{FAPI}/fapi/v1/depth",
            params={"symbol": symbol, "limit": 50},
            timeout=5
        )
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        bid_usdt = 0.0
        ask_usdt = 0.0

        for p, q in bids:
            try:
                bid_usdt += float(p) * float(q)
            except Exception:
                continue

        for p, q in asks:
            try:
                ask_usdt += float(p) * float(q)
            except Exception:
                continue

        if bid_usdt <= 0 and ask_usdt <= 0:
            ratio = 1.0
            label = "BALANCED (0 : 0)"
        elif ask_usdt <= 0:
            ratio = 10.0
            label = "BID DOM (∞ : 1)"
        elif bid_usdt <= 0:
            ratio = 0.1
            label = "ASK DOM (1 : ∞)"
        else:
            ratio = bid_usdt / ask_usdt
            if ratio >= 3.0:
                label = f"BID DOM ({ratio:.2f} : 1)"
            elif ratio <= (1/3):
                label = f"ASK DOM (1 : {1/ratio:.2f})"
            else:
                label = f"BALANCED ({ratio:.2f} : 1)"
    except Exception:
        ratio = 1.0
        label = "BALANCED (error)"

    orderbook_cache[symbol] = {"ts": now, "ratio": ratio, "label": label}
    return ratio, label

def classify_impulse_stage(vol_mult, volume_strength):
    try:
        vol_mult = float(vol_mult or 1.0)
        volume_strength = float(volume_strength or 0.0)
    except Exception:
        return "UNKNOWN"

    if (vol_mult < 4.0) and (volume_strength < 2.0):
        return "EARLY"
    if (vol_mult < 6.0 and volume_strength < 2.5):
        return "MID"
    return "LATE"

# ============================================================
# SIGNAL QUALITY SCORE (0–100)
# ============================================================

def compute_signal_quality(
    rsi,
    vol_mult,
    oi_chg,
    funding_label,
    price_spike_pct,
    price_pct,
    rsi_3m=None,
    ob_ratio=None
):
    try:
        score = 0.0

        if rsi is not None:
            rsi_component = rsi_momentum_component(rsi)
            score += rsi_component * 15

        if vol_mult is not None:
            try:
                vol_mult = float(vol_mult)
                vol_component = min(vol_mult / 5.0, 1.0)
                score += vol_component * 20
            except Exception:
                pass

        try:
            if oi_chg not in ["-", None]:
                oi_val = float(oi_chg)
                oi_component = min(abs(oi_val) / 5.0, 1.0)
                score += oi_component * 10
        except Exception:
            pass

        if funding_label not in ["PASS", "-", None]:
            if isinstance(funding_label, str) and funding_label.startswith("-"):
                score += 5
            else:
                score += 2.5

        if price_spike_pct is not None:
            try:
                spike_component = min(abs(float(price_spike_pct)) / 1.0, 1.0)
                score += spike_component * 20
            except Exception:
                pass

        if price_pct is not None:
            try:
                price_component = min(abs(float(price_pct)) / 5.0, 1.0)
                score += price_component * 10
            except Exception:
                pass

        if rsi_3m is not None:
            try:
                score += rsi_3m_trend_component(rsi_3m) * 10
            except Exception:
                pass

        if ob_ratio is not None:
            try:
                r = float(ob_ratio)
                if r <= 0:
                    ob_component = 0.0
                else:
                    ratio_norm = max(r, 1.0 / r)
                    ob_component = min(max((ratio_norm - 1.0) / 2.0, 0.0), 1.0)
                score += ob_component * 10
            except Exception:
                pass

        return int(round(score))
    except Exception:
        return 0

# ============================================================
# SPOT ADJUSTMENT (UNCHANGED)
# ============================================================

def compute_spot_adjustment(
    stage_label,
    direction,
    oi_chg,
    not_chg,
    acc_r,
    pos_r,
    glb_r,
    funding_change,
    wce_score
):
    if not SPOT_MODEL_ENABLED:
        return 0

    adj = 0

    if stage_label == "LATE":
        adj += SPOT_LATE_STAGE_PENALTY

    if isinstance(oi_chg, (int, float)) and isinstance(not_chg, (int, float)):
        if oi_chg > 0 and not_chg < 0:
            adj += SPOT_OI_UP_NOTIONAL_DOWN_PENALTY

    ratios = [r for r in (acc_r, pos_r, glb_r) if isinstance(r, (int, float))]
    avg_ls = None
    if ratios:
        avg_ls = sum(ratios) / len(ratios)

        if direction == "LONG" and avg_ls >= 1.6:
            adj += SPOT_RETAIL_CROWD_PENALTY
        if direction == "SHORT" and avg_ls <= 0.7:
            adj += SPOT_RETAIL_CROWD_PENALTY

    if (
        stage_label in ("EARLY", "MID")
        and isinstance(oi_chg, (int, float)) and oi_chg > 0
        and isinstance(not_chg, (int, float)) and not_chg > 0
        and (wce_score is None or wce_score >= 55)
    ):
        adj += SPOT_HEALTHY_CONT_BONUS

    try:
        fc = float(funding_change) if funding_change is not None else 0.0
    except Exception:
        fc = 0.0

    if (
        direction == "LONG"
        and stage_label in ("EARLY", "MID")
        and isinstance(oi_chg, (int, float)) and oi_chg > 0
        and isinstance(not_chg, (int, float)) and not_chg > 0
        and fc <= -0.10
        and (avg_ls is None or avg_ls <= 1.2)
        and (wce_score is None or wce_score >= 55)
    ):
        adj += SPOT_SHORT_SQUEEZE_BONUS

    adj = max(SPOT_ADJ_MAX_NEG, min(SPOT_ADJ_MAX_POS, adj))
    return int(adj)

# ============================================================
# SENTIMENT (OI, Notional, Long/Short, Funding)
# ============================================================

def fetch_sentiment_metrics(symbol):
    metrics = {
        "oi_now": "-", "oi_chg": "-",
        "not_now": "-", "not_chg": "-",
        "acc_r": "-", "pos_r": "-", "glb_r": "-",
        "last_f": "-", "funding_change": 0.0
    }

    try:
        base = f"{FAPI}/futures/data"

        oi = _http.get(
            f"{base}/openInterestHist",
            params={"symbol": symbol, "period": "15m", "limit": 2},
            timeout=6
        ).json()

        if isinstance(oi, list) and len(oi) >= 2:
            prev_oi = float(oi[0].get("sumOpenInterest", 0))
            curr_oi = float(oi[-1].get("sumOpenInterest", 0))
            prev_va = float(oi[0].get("sumOpenInterestValue", 0))
            curr_va = float(oi[-1].get("sumOpenInterestValue", 0))

            if prev_oi != 0:
                metrics["oi_chg"] = (curr_oi - prev_oi) / prev_oi * 100
            if prev_va != 0:
                metrics["not_chg"] = (curr_va - prev_va) / prev_va * 100

            metrics["oi_now"] = f"{int(curr_oi):,}"
            metrics["not_now"] = f"{int(curr_va):,}"

        def fetch_ratio(url):
            try:
                d = _http.get(url, timeout=6).json()
                if isinstance(d, list) and len(d) >= 1:
                    return float(d[-1].get("longShortRatio", 50.0))
            except Exception:
                pass
            return "-"

        metrics["acc_r"] = fetch_ratio(f"{base}/topLongShortAccountRatio?symbol={symbol}&period=15m&limit=1")
        metrics["pos_r"] = fetch_ratio(f"{base}/topLongShortPositionRatio?symbol={symbol}&period=15m&limit=1")
        metrics["glb_r"] = fetch_ratio(f"{base}/globalLongShortAccountRatio?symbol={symbol}&period=15m&limit=1")

        try:
            fund = _http.get(
                f"{base}/fundingRate",
                params={"symbol": symbol, "limit": 20},
                timeout=6
            ).json()

            current_f = None
            old_15m = None

            try:
                if isinstance(fund, list) and len(fund) >= 1:
                    x = fund[-1].get("fundingRate")
                    if x not in [None, ""]:
                        current_f = float(x)
            except Exception:
                pass

            try:
                if isinstance(fund, list) and len(fund) > 1:
                    ts_now = fund[-1].get("fundingTime", 0)
                    target_min = ts_now - 15 * 60 * 1000
                    candidates = [item for item in fund if item.get("fundingTime", 0) <= target_min]
                    if candidates:
                        x2 = candidates[-1].get("fundingRate")
                        if x2 not in [None, ""]:
                            old_15m = float(x2)
            except Exception:
                pass

            try:
                if current_f is not None and old_15m is not None:
                    metrics["funding_change"] = (current_f - old_15m) * 100.0
                else:
                    metrics["funding_change"] = 0.0
            except Exception:
                metrics["funding_change"] = 0.0

            def interval_map(v):
                v = round(v, 4)
                intervals = [-2, -1, -0.5, -0.1, 0, 0.1, 0.5, 1, 2]
                if v < intervals[0]:
                    return "< -2"
                if v >= intervals[-1]:
                    return "> 2"
                for i in range(len(intervals) - 1):
                    if intervals[i] <= v < intervals[i+1]:
                        return f"{intervals[i]} - {intervals[i+1]}"
                return "PASS"

            if current_f is not None:
                metrics["last_f"] = interval_map(current_f)
            elif old_15m is not None:
                metrics["last_f"] = interval_map(old_15m) + " (15m ago)"
            else:
                metrics["last_f"] = "PASS"

        except Exception:
            metrics["last_f"] = "PASS"
            metrics["funding_change"] = 0.0

    except Exception:
        metrics["last_f"] = "PASS"
        metrics["funding_change"] = 0.0

    def fmt_change(val):
        try:
            if val == "-" or val is None:
                return "-"
            return f"{val:+.1f}% {'↑' if val > 0 else '↓'}"
        except Exception:
            return "-"

    sentiment_text = (
        "\n📊 SENTIMENT\n"
        f"🔸 OI 15m: ({fmt_change(metrics['oi_chg'])}) {metrics['oi_now']}\n"
        f"🔸 Notional 15m: ({fmt_change(metrics['not_chg'])}) {metrics['not_now']}\n"
        f"🔸 Acct L/S: {metrics['acc_r']}\n"
        f"🔸 Pos L/S: {metrics['pos_r']}\n"
        f"🔸 Global L/S: {metrics['glb_r']}\n"
        f"🔸 Funding: {metrics['last_f']}"
    )
    return sentiment_text, metrics

def fetch_sentiment_cached(symbol, ttl=SENTIMENT_CACHE_TTL):
    now = time.time()
    entry = sentiment_cache.get(symbol)
    if entry and now - entry["ts"] < ttl:
        return entry["text"], entry["metrics"]
    text, metrics = fetch_sentiment_metrics(symbol)
    sentiment_cache[symbol] = {"ts": now, "text": text, "metrics": metrics}
    return text, metrics

# ============================================================
# WAVE CONFIRMATION ENGINE (WCE)
# ============================================================

def compute_wce(
    oi_change,
    notional_change,
    acc_ratio,
    pos_ratio,
    global_ratio,
    funding_change,
    rsi,
    price_pct,
    rsi_3m=None,
    ob_ratio=None
):
    try:
        oi_score = max(min(oi_change, 100.0), -100.0) if oi_change is not None else 0.0
        not_score = max(min(notional_change, 100.0), -100.0) if notional_change is not None else 0.0
        fund_score = max(min(funding_change, 100.0), -100.0) if funding_change is not None else 0.0

        ls_adv = (acc_ratio - 50.0) if isinstance(acc_ratio, (int, float)) else 0.0
        pos_adv = (pos_ratio - 50.0) if isinstance(pos_ratio, (int, float)) else 0.0
        glb_adv = (global_ratio - 50.0) if isinstance(global_ratio, (int, float)) else 0.0

        w_oi   = 0.25
        w_not  = 0.20
        w_ls   = 0.12
        w_pos  = 0.08
        w_fund = 0.08
        w_rsi1 = 0.10
        w_rsi3 = 0.07
        w_ob   = 0.05

        dir_factor = 1.0 if price_pct >= 0 else -1.0

        c_oi   = (oi_score / 100.0) * dir_factor
        c_not  = (not_score / 100.0) * dir_factor
        c_ls   = (ls_adv / 50.0) * dir_factor
        c_pos  = (pos_adv / 50.0) * dir_factor

        c_fund = (fund_score / 100.0) * (-1.0 if fund_score > 0 else 1.0)

        if rsi is None:
            c_rsi1 = 0.0
        else:
            c_rsi1 = (1.0 - abs(rsi - 50.0)/50.0)*2 - 1

        if rsi_3m is None:
            c_rsi3 = 0.0
        else:
            c_rsi3 = (1.0 - abs(rsi_3m - 50.0)/50.0)*2 - 1

        if ob_ratio is None or ob_ratio <= 0:
            c_ob = 0.0
        else:
            ratio_norm = max(ob_ratio, 1.0/ob_ratio)
            ob_adv = min(max((ratio_norm - 1.0) / 2.0, 0.0), 1.0)
            c_ob = ob_adv * dir_factor

        raw = (
            w_oi   * c_oi   +
            w_not  * c_not  +
            w_ls   * c_ls   +
            w_pos  * c_pos  +
            w_fund * c_fund +
            w_rsi1 * c_rsi1 +
            w_rsi3 * c_rsi3 +
            w_ob   * c_ob
        )

        raw = max(min(raw, 1.0), -1.0)
        score = int(round((raw + 1) / 2 * 100))

        if price_pct > 0:
            trend = "LONG" if acc_ratio > 55 or pos_ratio > 55 else "LONG (weak)" if score >= 50 else "LONG (uncertain)"
        else:
            trend = "SHORT" if acc_ratio < 45 or pos_ratio < 45 else "SHORT (weak)" if score >= 50 else "SHORT (uncertain)"

        fake = "HIGH" if oi_change < -3 and notional_change < -3 and abs(price_pct) >= 5 else \
               "MEDIUM" if oi_change < 0 and notional_change < 0 and abs(price_pct) >= 3 else \
               "LOW" if score >= 60 else "MEDIUM" if score >= 40 else "HIGH"

        conf = "HIGH" if score >= 80 else "MEDIUM" if score >= 60 else "LOW"

        text = (
            f"\n🔥 Wave Confirmation\n"
            f"Score: {score}%\n"
            f"Trend direction: {trend}\n"
            f"Fake-out risk: {fake}\n"
            f"Confidence: {conf}"
        )
        return score, trend, fake, conf, text

    except Exception as e:
        print("compute_wce error:", e)
        return 50, "UNKNOWN", "MEDIUM", "LOW", "\n🔥 Wave Confirmation: unavailable"

# ============================================================
# PATTERN-BASED SENTIMENT ENGINE (Variant C)
# ============================================================

def generate_pattern_analysis(
    metrics,
    price_pct,
    rsi=None,
    rsi_3m=None,
    ob_ratio=None,
    ob_label=None,
    stage_label=None,
    mode="full"
):
    try:
        def to_float(val, default=None):
            try:
                if val in ["-", None]:
                    return default
                return float(val)
            except Exception:
                return default

        oi_chg = to_float(metrics.get("oi_chg"), 0.0)
        not_chg = to_float(metrics.get("not_chg"), 0.0)
        acc_r = to_float(metrics.get("acc_r"), None)
        pos_r = to_float(metrics.get("pos_r"), None)
        glb_r = to_float(metrics.get("glb_r"), None)

        price_pct = price_pct or 0.0
        abs_p = abs(price_pct)

        if oi_chg is None:
            oi_label = "Flat / unknown"
        elif oi_chg > 2:
            oi_label = "Increasing (leverage coming in)"
        elif oi_chg < -2:
            oi_label = "Decreasing (positions closing)"
        else:
            oi_label = "Stable / sideway"

        if pos_r is None:
            pro_label = "Neutral / unknown"
        elif pos_r > 1.1:
            pro_label = "Long-biased (Pos L/S > 1)"
        elif pos_r < 0.9:
            pro_label = "Short-biased (Pos L/S < 1)"
        else:
            pro_label = "Balanced"

        if acc_r is None:
            retail_label = "Neutral / unknown"
        elif acc_r > 1.1:
            retail_label = "Long-heavy (FOMO risk)"
        elif acc_r < 0.9:
            retail_label = "Short-heavy (squeeze fuel)"
        else:
            retail_label = "Balanced / mixed"

        if glb_r is None:
            global_label = "Neutral"
        elif glb_r > 1.05:
            global_label = "Bullish-leaning"
        elif glb_r < 0.95:
            global_label = "Bearish-leaning"
        else:
            global_label = "Neutral"

        if abs_p < MOMENTUM_THRESHOLD * 0.5:
            momentum_label = "Weak / choppy"
        elif abs_p < MOMENTUM_THRESHOLD * 1.2:
            momentum_label = "Building"
        else:
            momentum_label = "Strong move"

        if price_pct > 0 and oi_chg is not None and oi_chg < 0:
            divergence_label = "Price↑ & OI↓ → Short squeeze potential"
            squeeze_bias = 2
        elif price_pct < 0 and oi_chg is not None and oi_chg > 0:
            divergence_label = "Price↓ & OI↑ → Leverage trap / breakdown risk"
            squeeze_bias = -1
        else:
            divergence_label = "Price & OI aligned"
            squeeze_bias = 0

        if price_pct > 0:
            bias = "LONG"
            trend_dir_label = "Bullish"
        elif price_pct < 0:
            bias = "SHORT"
            trend_dir_label = "Bearish"
        else:
            bias = "NONE"
            trend_dir_label = "Sideways"

        fake_score = 0
        if bias == "LONG" and acc_r is not None and acc_r > 1.1 and glb_r is not None and glb_r > 1.05:
            fake_score += 2
        if bias == "SHORT" and acc_r is not None and acc_r < 0.9 and glb_r is not None and glb_r < 0.95:
            fake_score += 2
        if abs_p >= PATTERN_PCT and (oi_chg is not None and abs(oi_chg) < 1.0):
            fake_score += 1
        if squeeze_bias > 0 and bias == "LONG":
            fake_score = max(fake_score - 1, 0)

        if fake_score <= 0:
            fake_label = "LOW"
        elif fake_score <= 2:
            fake_label = "MEDIUM"
        else:
            fake_label = "HIGH"

        squeeze_prob = "LOW"
        if (
            price_pct > 0
            and oi_chg is not None and oi_chg <= 0
            and acc_r is not None and acc_r < 0.9
            and glb_r is not None and glb_r < 0.9
        ):
            squeeze_prob = "HIGH"
        elif squeeze_bias > 0:
            squeeze_prob = "MEDIUM"

        score = 0.0
        score += min(abs_p / PATTERN_PCT, 2.0) * 20

        if oi_chg is not None:
            same_dir = (price_pct > 0 and oi_chg > 0) or (price_pct < 0 and oi_chg < 0)
            if same_dir:
                score += 20
            elif squeeze_bias != 0:
                score += 10

        if bias == "LONG" and pos_r is not None and pos_r > 1.05:
            score += 15
        if bias == "SHORT" and pos_r is not None and pos_r < 0.95:
            score += 15

        if bias == "LONG" and acc_r is not None and acc_r < 0.9:
            score += 10
        if bias == "SHORT" and acc_r is not None and acc_r > 1.1:
            score += 10

        if momentum_label == "Strong move":
            score += 10

        if fake_label == "HIGH":
            score -= 20
        elif fake_label == "MEDIUM":
            score -= 5

        score = max(0, min(int(round(score)), 100))

        if score < 40 or bias == "NONE":
            final_bias = "NO TRADE"
            icon = "🟧"
        else:
            final_bias = bias
            icon = "🟢" if bias == "LONG" else "🔴"

        if trend_dir_label == "Bullish":
            verdict = "Bullish & Strong" if momentum_label == "Strong move" else \
                      "Bullish but Weak" if momentum_label == "Weak / choppy" else \
                      "Bullish (developing)"
        elif trend_dir_label == "Bearish":
            verdict = "Bearish & Strong" if momentum_label == "Strong move" else \
                      "Bearish but Weak" if momentum_label == "Weak / choppy" else \
                      "Bearish (developing)"
        else:
            verdict = "Sideways / No clear trend"

        reasons = []
        if oi_chg is not None:
            if price_pct > 0 and oi_chg > 0:
                reasons.append("OI↑ + Price↑ (real trend)")
            elif price_pct < 0 and oi_chg < 0:
                reasons.append("OI↓ + Price↓ (real unwinding)")
        if pos_r is not None:
            if bias == "LONG" and pos_r > 1.05:
                reasons.append("pro accumulation")
            if bias == "SHORT" and pos_r < 0.95:
                reasons.append("pro short bias")
        if acc_r is not None:
            if acc_r > 1.1:
                reasons.append("retail FOMO same side")
            elif acc_r < 0.9:
                reasons.append("retail opposite (squeeze fuel)")
        if ob_ratio is not None and ob_label:
            if ob_ratio > 1.3 and bias == "LONG":
                reasons.append("bid-side orderbook support")
            if ob_ratio < (1/1.3) and bias == "SHORT":
                reasons.append("ask-side orderbook pressure")

        if not reasons:
            reasons.append("mixed sentiment, low conviction")

        reason_line = ", ".join(reasons)

        lines = ["\n📊 PATTERN SUMMARY"]
        if mode == "full":
            lines.append(f"• OI Pattern: {oi_label}")
            lines.append(f"• Pro Traders: {pro_label}")
            lines.append(f"• Retail: {retail_label}")
            lines.append(f"• Global Sentiment: {global_label}")
            lines.append(f"• Momentum Context: {momentum_label}")
            lines.append(f"• Divergence: {divergence_label}")
        elif mode == "short":
            lines.append(f"• OI: {oi_label}")
            lines.append(f"• Pro: {pro_label}")
            lines.append(f"• Retail: {retail_label}")
            lines.append(f"• Divergence: {divergence_label}")
        else:
            lines.append(f"• OI: {oi_label}")
            lines.append(f"• Retail: {retail_label}")
            lines.append(f"• Divergence: {divergence_label}")

        lines.append(f"\n🎯 TREND VERDICT: {verdict}")
        lines.append(f"Fake-out risk: {fake_label}")
        lines.append(f"Squeeze probability: {squeeze_prob}")
        lines.append("\n🔮 FINAL CONFIRMATION:")
        lines.append(f"{icon} {final_bias} — {score}%")
        lines.append(f"Reason: {reason_line}")

        return "\n".join(lines)

    except Exception as e:
        print("generate_pattern_analysis error:", e)
        return ""

# ============================================================
# TELEGRAM (async queue)
# ============================================================

def _send_telegram_sync(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secrets not set.")
        return False
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": escape_md(text),
            "parse_mode": "MarkdownV2"
        }
        r = _http.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10
        )
        if r.ok:
            print("📩 Telegram sent")
            return True
        else:
            print("❌ Telegram failed:", r.status_code, r.text)
            return False
    except Exception as e:
        print("Telegram error:", e)
        return False

def send_telegram(text):
    try:
        telegram_queue.put_nowait(text)
        return True
    except Full:
        print("⚠️ Telegram queue full, dropping message")
        return False

def telegram_worker():
    while True:
        try:
            text = telegram_queue.get()
            _send_telegram_sync(text)
        except Exception as e:
            print("telegram_worker error:", e)
        finally:
            try:
                telegram_queue.task_done()
            except:
                pass

# ============================================================
# START FULL (heavy) — moved off WS thread
# ============================================================

def run_start_full(snapshot):
    symbol = snapshot["symbol"]
    pct_15m = snapshot["pct_15m"]
    vol_mult = snapshot["vol_mult"]
    volume_strength = snapshot["volume_strength"]
    short_pct = snapshot["short_pct"]
    stage_label = snapshot["stage_label"]

    closes = get_closes(symbol, limit=100, interval="1m")
    rsi = compute_rsi(closes, RSI_PERIOD)

    sentiment_text, metrics = fetch_sentiment_cached(symbol)
    rsi3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
    ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)

    vol24 = get_24h_volume_cached(symbol)      

    base_q = compute_signal_quality(
        rsi,
        vol_mult,
        metrics.get("oi_chg"),
        metrics.get("last_f"),
        short_pct,
        pct_15m,
        rsi3m,
        ob_ratio
    )

    wce_score, _, _, _, wce_text = compute_wce(
        metrics.get("oi_chg", 0.0),
        metrics.get("not_chg", 0.0),
        metrics.get("acc_r", 50.0),
        metrics.get("pos_r", 50.0),
        metrics.get("glb_r", 50.0),
        metrics.get("funding_change", 0.0),
        rsi,
        pct_15m,
        rsi3m,
        ob_ratio
    )

    direction = "LONG" if pct_15m > 0 else "SHORT"

    spot_adj = compute_spot_adjustment(
        stage_label,
        direction,
        metrics.get("oi_chg"),
        metrics.get("not_chg"),
        metrics.get("acc_r"),
        metrics.get("pos_r"),
        metrics.get("glb_r"),
        metrics.get("funding_change"),
        wce_score
    )

    signal_q = max(0, min(100, base_q + spot_adj))

    pattern_block = generate_pattern_analysis(
        metrics,
        pct_15m,
        rsi=rsi,
        rsi_3m=rsi3m,
        ob_ratio=ob_ratio,
        ob_label=ob_label,
        stage_label=stage_label,
        mode="full"
    )

    caption = (
        "🚀 START\n"
        f"{symbol}\n\n"
        f"📈 Change (15m): {pct_15m:+.2f}%\n"
        f"💰 Price: {snapshot.get('price','-')}\n"
        f"📊 Volume spike (1m/5m): ×{vol_mult:.2f}\n"
        f"💪 Volume Strength (15m/15m): {volume_strength:.2f}x\n"
        f"⚡ Micro Spike (short): {short_pct:+.2f}%\n"
        f"⏳ Impulse Stage: {stage_label}\n"
        f"📦 24h Volume: {vol24:,.0f} USDT\n"
        f"📉 RSI(1m): {rsi}\n"
        f"📉 RSI(3m): {rsi3m} ({rsi3m_trend})\n"
        f"📊 Orderbook: {ob_label}\n"
        f"{sentiment_text}\n"
        f"🔍 Signal Quality: {signal_q}/100\n"
        f"• Base: {base_q}\n"
        f"• Spot Adj: {spot_adj:+d}\n\n"
        f"{pattern_block}\n\n"
        f"{wce_text}"
    )

    send_telegram(caption)

    log_signal("START", {
        "symbol": symbol,
        "pct_15m": pct_15m,
        "vol_mult": vol_mult,
        "signal_q": signal_q,
        "direction": direction
    })

    try:
        e = state.get(symbol)
        if e:
            e["last_wce"] = wce_score
            e["last_signal_q"] = signal_q
            e["last_rsi3m_trend"] = rsi3m_trend
    except:
        pass

# ============================================================
# EXIT FULL (heavy) — moved off WS thread
# ============================================================

def run_exit_full(snapshot):
    symbol = snapshot["symbol"]
    pct_15m = snapshot["pct_15m"]
    vol_mult = snapshot["vol_mult"]
    volume_strength = snapshot["volume_strength"]
    short_pct = snapshot["short_pct"]
    recent_1m = snapshot["recent_1m"]
    baseline_avg_1m = snapshot["baseline_avg_1m"]
    now_ts = snapshot["now_ts"]

    entry = state.get(symbol)
    if not entry:
        return

    # Re-check guards (same meaning, just safe)
    start_time = entry.get("start_time")
    if not start_time or entry.get("phase") != "ACTIVE":
        return
    if now_ts - start_time < EXIT_MIN_AGE:
        return

    last_check = entry.get("last_exit_check", 0.0)
    if now_ts - last_check < EXIT_CHECK_INTERVAL:
        return
    entry["last_exit_check"] = now_ts

    vol_drop = 0.0
    if baseline_avg_1m and baseline_avg_1m > 0:
        try:
            vol_drop = max(0.0, 1.0 - (recent_1m / baseline_avg_1m))
        except Exception:
            vol_drop = 0.0

    closes = get_closes(symbol, limit=100, interval="1m")
    rsi = compute_rsi(closes, RSI_PERIOD)

    _, metrics = fetch_sentiment_cached(symbol)
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

    wce_score, wce_trend, _, _, _ = compute_wce(
        metrics.get("oi_chg", 0.0) if metrics.get("oi_chg") not in ["-", None] else 0.0,
        metrics.get("not_chg", 0.0) if metrics.get("not_chg") not in ["-", None] else 0.0,
        metrics.get("acc_r", 50.0) if isinstance(metrics.get("acc_r"), (int, float)) else 50.0,
        metrics.get("pos_r", 50.0) if isinstance(metrics.get("pos_r"), (int, float)) else 50.0,
        metrics.get("glb_r", 50.0) if isinstance(metrics.get("glb_r"), (int, float)) else 50.0,
        metrics.get("funding_change", 0.0),
        rsi,
        pct_15m,
        rsi_3m=rsi3m if rsi3m is not None else 50.0,
        ob_ratio=ob_ratio
    )

    prev_wce = entry.get("last_wce", wce_score)
    prev_rsi3m_trend = entry.get("last_rsi3m_trend")
    direction = entry.get("direction", "UNKNOWN")

    reasons = []

    if prev_wce - wce_score >= EXIT_WCE_DROP:
        reasons.append(f"WCE drop {prev_wce:.0f} → {wce_score:.0f}")

    if vol_drop >= EXIT_VOLUME_DROP:
        reasons.append(f"Volume collapse {vol_drop*100:.0f}%")

    if direction == "LONG" and short_pct <= -EXIT_MICRO_REVERSE:
        reasons.append("Micro reverse vs LONG")
    elif direction == "SHORT" and short_pct >= EXIT_MICRO_REVERSE:
        reasons.append("Micro reverse vs SHORT")

    if EXIT_USE_RSI3M_FLIP and prev_rsi3m_trend and rsi3m_trend != prev_rsi3m_trend:
        reasons.append(f"RSI(3m) flip {prev_rsi3m_trend} → {rsi3m_trend}")

    if len(reasons) < 2:
        entry["last_wce"] = wce_score
        entry["last_rsi3m_trend"] = rsi3m_trend
        entry["last_signal_q"] = signal_q
        return

    caption = (
        "⚠ EXIT\n"
        f"{symbol}\n\n"
        f"Direction: {direction} → {wce_trend}\n"
        f"Price(15m): {pct_15m:+.2f}%\n"
        f"SignalQ: {signal_q}/100\n\n"
        f"Reasons:\n" + "\n".join(f"• {r}" for r in reasons)
    )

    if send_telegram(caption):
        entry["phase"] = "EXITED"
        entry["tracking"] = False
        entry["exit_sent_at"] = now_ts
        entry["last_notify"] = None

        log_signal("EXIT", {
            "symbol": symbol,
            "direction": direction,
            "pct_15m": pct_15m,
            "signal_q": signal_q,
            "reasons": reasons
        })

    entry["last_wce"] = wce_score
    entry["last_rsi3m_trend"] = rsi3m_trend
    entry["last_signal_q"] = signal_q

# ============================================================
# ANALYSIS WORKER (ADDIM 5)
# ============================================================

def analysis_worker():
    while True:
        try:
            task, snapshot = task_queue.get()
            if task == "START_FULL":
                run_start_full(snapshot)
            elif task == "EXIT_FULL":
                run_exit_full(snapshot)
        except Exception as e:
            print("analysis_worker error:", e)
        finally:
            try:
                task_queue.task_done()
            except:
                pass

# ============================================================
# WEBSOCKET CORE — REALTIME ENGINE (FAST)
# ============================================================

def _process_mini(msg):
    global last_any_msg_ts

    symbol = msg.get("s") or msg.get("symbol")
    if not symbol or not symbol.endswith("USDT"):
        return

    now = time.time()
    last_any_msg_ts = now

    if tracked_syms and symbol not in tracked_syms:
        return

    price = float(msg.get("c", 0) or 0)
    vol = float(msg.get("q", msg.get("v", 0)) or 0)

    if price <= 0:
        return

    entry = state.setdefault(symbol, {
        "prices": [],
        "vols": [],
        "last_v": None,
        "tracking": False,
        "phase": "IDLE",
        "last_notify": None    
    })

    entry["prices"].append(price)

    if entry["last_v"] is None:
        diff_vol = 0.0
    else:
        diff_vol = max(vol - entry["last_v"], 0.0)
    entry["last_v"] = vol
    entry["vols"].append(diff_vol)

    entry["prices"] = entry["prices"][-1800:]
    entry["vols"] = entry["vols"][-1800:]

    last_seen[symbol] = now

    prices = entry["prices"]
    plen = len(prices)

    if plen <= LOOKBACK_MIN * 60:
        return
    if plen <= SHORT_WINDOW:
        return

    price_15m_ago = prices[-LOOKBACK_MIN * 60]
    pct_15m = (price - price_15m_ago) / price_15m_ago * 100 if price_15m_ago else 0.0

    recent_1m = sum(entry["vols"][-60:])
    prev_5m = sum(entry["vols"][-360:-60]) or 1
    baseline_avg_1m = prev_5m / 5
    vol_mult = recent_1m / baseline_avg_1m if baseline_avg_1m > 0 else 1.0

    volume_strength = (
        sum(entry["vols"][-900:]) /
        max(sum(entry["vols"][-1800:-900]), 1)
    )

    short_base = prices[-SHORT_WINDOW]
    short_pct = (price - short_base) / short_base * 100 if short_base else 0.0

    now_ts = now

    # ========================================================
    # STEP — TELEMETRY (NO SIGNAL)
    # ========================================================
    if entry.get("phase") == "ACTIVE":
        last_step_price = entry.get("last_step_price", price)
        pct_from_last = (price - last_step_price) / last_step_price * 100 if last_step_price else 0.0

        if (
            abs(pct_from_last) >= STEP_PCT
            and vol_mult >= STEP_VOLUME_SPIKE
            and volume_strength >= STEP_VOLUME_STRENGTH
        ):
            entry["last_step_price"] = price

            prev = entry.get("last_notify")

            # --- Δ vs previous notification (MAIN telemetry) ---
            if prev:
                dp_pct = (price - prev["price"]) / prev["price"] * 100 if prev.get("price") else 0.0
                d_vol = vol_mult - prev.get("vol_mult", vol_mult)
                d_str = volume_strength - prev.get("volume_strength", volume_strength)
            else:
                dp_pct = 0.0
                d_vol = 0.0
                d_str = 0.0

            # --- Context vs START (optional, informational only) ---
            start_price = entry.get("start_price")
            from_start = (
                (price - start_price) / start_price * 100
                if start_price else 0.0
            )

            caption = (
                "📡 STEP — Telemetry\n"
                f"{symbol}\n\n"
                "Δ vs Previous\n"
                f"• Price: {dp_pct:+.2f}%\n"
                f"• Vol spike: {d_vol:+.2f}\n"
                f"• Vol strength: {d_str:+.2f}\n\n"
                "Context\n"
                f"• 15m total: {pct_15m:+.2f}%\n"
                f"• From START: {from_start:+.2f}%"
            )

            send_telegram(caption)

            # --- Update telemetry reference ---
            entry["last_notify"] = {
                "price": price,
                "pct_15m": pct_15m,
                "vol_mult": vol_mult,
                "volume_strength": volume_strength,
                "ts": now_ts
            }

            log_signal("STEP", {
                "symbol": symbol,
                "pct_15m": pct_15m,
                "delta_price_pct": dp_pct,
                "delta_vol_mult": d_vol,
                "delta_volume_strength": d_str
            })

        # EXIT — enqueue only (UNCHANGED)
        if EXIT_ENABLED:
            start_time = entry.get("start_time")
            if start_time and (now_ts - start_time >= EXIT_MIN_AGE):
                last_check = entry.get("last_exit_check", 0.0)
                if now_ts - last_check >= EXIT_CHECK_INTERVAL:
                    snapshot = {
                        "symbol": symbol,
                        "pct_15m": pct_15m,
                        "vol_mult": vol_mult,
                        "volume_strength": volume_strength,
                        "short_pct": short_pct,
                        "recent_1m": recent_1m,
                        "baseline_avg_1m": baseline_avg_1m,
                        "now_ts": now_ts
                    }
                    try:
                        task_queue.put_nowait(("EXIT_FULL", snapshot))
                    except Full:
                        print("⚠️ task_queue full, EXIT dropped:", symbol)

        return

    # ========================================================
    # START — FULL POWER (ENQUEUE ONLY)
    # ========================================================
    if (
        abs(pct_15m) >= START_PCT
        and vol_mult >= START_VOLUME_SPIKE
        and abs(short_pct) >= START_MICRO_PCT

        # --- FAKE SPIKE PROTECTION (RESTORED) ---
        and volume_strength >= FAKE_VOLUME_STRENGTH
        and recent_1m >= FAKE_RECENT_MIN_USDT
        and (vol_mult <= 50 or recent_1m >= FAKE_RECENT_STRONG_USDT)
    ):

        entry["phase"] = "ACTIVE"
        entry["tracking"] = True
        entry["start_price"] = price
        entry["last_step_price"] = price
        entry["start_time"] = now_ts
        entry["direction"] = "LONG" if pct_15m > 0 else "SHORT"
        entry["last_notify"] = None

        snapshot = {
            "symbol": symbol,
            "price": price,
            "pct_15m": pct_15m,
            "vol_mult": vol_mult,
            "volume_strength": volume_strength,
            "short_pct": short_pct,
            "stage_label": classify_impulse_stage(vol_mult, volume_strength),
            "now_ts": now_ts
        }

        try:
            task_queue.put_nowait(("START_FULL", snapshot))

            entry["last_notify"] = {
                "price": price,
                "pct_15m": pct_15m,
                "vol_mult": vol_mult,
                "volume_strength": volume_strength,
                "ts": now_ts
            }

        except Full:
            print("⚠️ task_queue full, START dropped:", symbol)

# ============================================================
# HANDLE MINITICKER (unwrap 'data' if present)
# ============================================================

def handle_miniticker(msg):
    try:
        if msg is None:
            return

        # Some python-binance versions wrap payload like: {"stream": "...", "data": [...]}
        if isinstance(msg, dict) and "data" in msg:
            msg = msg["data"]

        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    _process_mini(item)
        elif isinstance(msg, dict):
            _process_mini(msg)
    except Exception as e:
        print("handle_miniticker error:", e)

# ============================================================
# WEBSOCKET MONITOR (FUTURES MULTIPLEX — python-binance 1.0.19)
# ============================================================

def _start_miniticker_socket(twm: ThreadedWebsocketManager):
    streams = ["!miniTicker@arr"]
    twm.start_futures_multiplex_socket(
        streams=streams,
        callback=handle_miniticker
    )
    print("📡 Subscribed to FUTURES MINITICKER multiplex stream.")

def ws_monitor(min_active=10, check_interval=30):
    global ws_manager

    while True:
        try:
            now = time.time()

            min_req = min(min_active, max(2, len(tracked_syms)//3)) if tracked_syms else min_active
            active = sum(1 for s in tracked_syms if last_seen.get(s, 0) > now - 90)

            if active < min_req:
                print(f"⚠ WS monitor: {active}/{min_req} active — reconnecting WS")

                try:
                    if ws_manager:
                        ws_manager.stop()
                        time.sleep(2)
                except:
                    pass

                try:
                    twm = ThreadedWebsocketManager(
                        api_key=BINANCE_API_KEY,
                        api_secret=BINANCE_API_SECRET
                    )
                    twm.start()
                    ws_manager = twm
                    _start_miniticker_socket(twm)
                    print("🔁 WebSocket reconnected")
                except Exception as e:
                    print("WS reconnect failed:", e)

            time.sleep(check_interval)

        except Exception as e:
            print("ws_monitor error:", e)
            time.sleep(check_interval)

# ============================================================
# HEARTBEAT
# ============================================================

def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def heartbeat_loop():
    global last_heartbeat_ts
    if not HEARTBEAT_ENABLED:
        return

    while True:
        try:
            now = time.time()

            if last_heartbeat_ts == 0:
                last_heartbeat_ts = now

            if now - last_heartbeat_ts >= HEARTBEAT_INTERVAL:
                uptime = now - START_TIME
                active = sum(1 for s in tracked_syms if last_seen.get(s, 0) > now - 90)
                total = len(tracked_syms)
                last_msg_age = now - last_any_msg_ts if last_any_msg_ts > 0 else None

                ws_status = "OK ✅" if ws_manager is not None else "NONE ⚠️"
                tick_text = f"{int(last_msg_age)}s" if last_msg_age is not None else "N/A"

                hb_text = (
                    "🤖 Scanner Alive (Railway)\n"
                    f"Tracked pairs: {total}\n"
                    f"Active (last 90s): {active}\n"
                    f"Uptime: {format_uptime(uptime)}\n"
                    f"WS: {ws_status}\n"
                    f"Last tick age: {tick_text}"
                )

                send_telegram(hb_text)
                last_heartbeat_ts = now

            time.sleep(15)

        except Exception as e:
            print("heartbeat_loop error:", e)
            time.sleep(30)

# ============================================================
# WATCHDOG
# ============================================================

def watchdog_loop():
    if not WATCHDOG_ENABLED:
        return

    while True:
        try:
            now = time.time()
            if last_any_msg_ts == 0:
                time.sleep(60)
                continue

            idle = now - last_any_msg_ts
            uptime = now - START_TIME

            if idle > WATCHDOG_NO_MSG_TIMEOUT and uptime > WATCHDOG_MIN_UPTIME:
                msg = (
                    f"⚠ Watchdog: no miniticker for {int(idle)}s, "
                    f"uptime {format_uptime(uptime)} — restarting scanner (Railway will respawn)."
                )
                print(msg)
                try:
                    send_telegram(msg)
                except:
                    pass
                os._exit(1)

        except Exception as e:
            print("watchdog_loop error:", e)

        time.sleep(60)

# ============================================================
# START STREAM
# ============================================================

def start_stream():
    global ws_manager, tracked_syms

    print("🔍 Loading symbols...")

    try:
        info = client.futures_exchange_info()
        syms = [s["symbol"] for s in info["symbols"]
                if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]

        vol_list = [(s, get_24h_volume_cached(s)) for s in syms]
        vol_list = [(s, v) for s, v in vol_list if v >= MIN24H]

        vol_list.sort(key=lambda x: x[1], reverse=True)
        top_syms = [s for s, v in vol_list[:TOP_N]]

        print(f"✅ Found {len(syms)} USDT futures symbols.")
        print(f"📌 After MIN24H filter: {len(top_syms)}")
        print("🚀 TRACKING:", top_syms)

    except Exception as e:
        print("Symbol load error:", e)
        return

    if not top_syms:
        print("⚠ No symbols to track! Increase MIN24H.")
        return

    tracked_syms = set(top_syms)

    threading.Thread(
        target=warmup_vol24,
        args=(list(tracked_syms),),
        daemon=True
    ).start()

    try:
        twm = ThreadedWebsocketManager(
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_API_SECRET
        )
        twm.start()
        ws_manager = twm

        _start_miniticker_socket(twm)

    except Exception as e:
        print("❌ Failed to start miniticker socket:", e)
        return

    threading.Thread(target=ws_monitor, daemon=True).start()
    print("🚀 Scanner started (WebSocket + Monitor)")

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("🚀 SCANNER STARTING (SCANNER PRO)")

    # Start workers FIRST
    for _ in range(max(1, TELEGRAM_WORKERS)):
        threading.Thread(target=telegram_worker, daemon=True).start()

    for _ in range(max(1, ANALYSIS_WORKERS)):
        threading.Thread(target=analysis_worker, daemon=True).start()

    threading.Thread(target=start_stream, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()

    try:
        send_telegram("🚀 Scanner started")
    except:
        pass

    while True:
        time.sleep(5)

