# ============================================================
#   Binance Futures Scanner — Railway Version
#   START / STEP / EXIT / REVERSE — FULL ORIGINAL LOGIC
#   + Heartbeat (Alive ping to Telegram)
#   + Signal Logging (START / STEP / EXIT / REVERSE)
#   + Watchdog (auto-restart if data stops)
#   7/24 stable Railway execution
# ============================================================

import os
import time
import math
import json
import threading
import requests
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from binance import ThreadedWebsocketManager
from binance.client import Client

import functools
print = functools.partial(print, flush=True)

# ============================================================
# GLOBAL STATES
# ============================================================

last_log = 0
state = {}             # per-symbol signal tracking
last_seen = {}         # symbol timestamp monitor
notified = {}
tracked_syms = set()

# API caches
sentiment_cache = {}
rsi3m_cache = {}
orderbook_cache = {}
vol24_cache = {}

# Uptime / monitor states
START_TIME = time.time()
last_any_msg_ts = 0.0
last_start_ts = 0.0
last_heartbeat_ts = 0.0

# ============================================================
# CONFIG (UNCHANGED + NEW FEATURES)
# ============================================================

THRESHOLD = 7.0
VOLUME_SPIKE = 3.0
VOLUME_STRENGTH_MIN = 1.5
MIN24H = 7_000_000
PRICE_SPIKE_MIN = 0.15

STEP_PCT = 5.0
STEP_VOLUME_SPIKE = 3.0
STEP_VOLUME_STRENGTH = 1.3

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
MIN_RECENT_VOLUME_USDT = 1500

RSI3M_CACHE_TTL = 20
ORDERBOOK_CACHE_TTL = 10
SENTIMENT_CACHE_TTL = 30
VOL24_CACHE_TTL = 300

# --- NEW: Heartbeat / Logging / Watchdog config ---
HEARTBEAT_ENABLED = True
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "1800"))  # 30 dəq default

LOG_ENABLED = True
LOG_FILE = os.getenv("SIGNAL_LOG_FILE", "signals.log")

WATCHDOG_ENABLED = True
WATCHDOG_NO_MSG_TIMEOUT = int(os.getenv("WATCHDOG_NO_MSG_TIMEOUT", "900"))  # 15 dəq
WATCHDOG_MIN_UPTIME = 300  # ilk 5 dəqiqədə restart etməsin

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
# MARKDOWN ESCAPE
# ============================================================

def escape_md(text):
    if not text:
        return ""
    chars = r"\_[]()#+-=!.>"
    for c in chars:
        text = text.replace(c, "\\" + c)
    return text

# ============================================================
# LOGGING HELPERS
# ============================================================

def log_signal(event_type, data: dict):
    """
    START / STEP / EXIT / REVERSE eventlərini JSON line olaraq fayla yazır.
    """
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
        r = requests.get(f"{FAPI}/fapi/v1/ticker/24hr", params={"symbol": symbol}, timeout=8)
        return float(r.json().get("quoteVolume", 0))
    except:
        return 0.0

def get_24h_volume_cached(symbol):
    now = time.time()
    e = vol24_cache.get(symbol)
    if e and now - e["ts"] < VOL24_CACHE_TTL:
        return e["value"]
    v = get_24h_volume(symbol)
    vol24_cache[symbol] = {"ts": now, "value": v}
    return v

def get_closes(symbol, limit=100, interval="1m"):
    try:
        r = requests.get(
            f"{FAPI}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=8
        )
        data = r.json()
        if isinstance(data, list):
            return [float(k[4]) for k in data]
        return []
    except:
        return []

def compute_rsi(prices, period=14):
    try:
        if len(prices) <= period:
            return None
        arr = np.array(prices)
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
    except:
        return None

# ============================================================
# RSI 3m + ORDERBOOK + IMPULSE STAGE
# ============================================================

def fetch_rsi_3m_cached(symbol, ttl=RSI3M_CACHE_TTL):
    """
    Fetch 3m RSI(14) for trend confirmation, cached to reduce API load.
    Returns (rsi_3m, trend_label)
    """
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
    """
    Fetch orderbook depth and compute BID/ASK imbalance.
    Returns (ratio, label) where ratio = bid_usdt / ask_usdt.
    """
    now = time.time()
    entry = orderbook_cache.get(symbol)
    if entry and now - entry["ts"] < ttl:
        return entry["ratio"], entry["label"]

    try:
        r = requests.get(
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
    """
    Classify impulse as EARLY / MID / LATE based on volume spike and strength.
    """
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
    """
    RSI + Volume Spike + OI + Funding + Price Spike + Price Move
    + RSI 3m trend + Orderbook imbalance
    birlikdə 0–100 bal arasında 'Signal Quality Score' qaytarır.
    """
    try:
        score = 0.0

        # 1) RSI stability (15%)
        if rsi is not None:
            rsi_component = max(0.0, 1 - abs(rsi - 50) / 50)
            score += rsi_component * 15

        # 2) Volume Spike (20%)
        if vol_mult is not None:
            try:
                vol_mult = float(vol_mult)
                vol_component = min(vol_mult / 5.0, 1.0)
                score += vol_component * 20
            except Exception:
                pass

        # 3) OI Change (10%)
        try:
            if oi_chg not in ["-", None]:
                oi_val = float(oi_chg)
                oi_component = min(abs(oi_val) / 5.0, 1.0)
                score += oi_component * 10
        except Exception:
            pass

        # 4) Funding Rate (5%)
        if funding_label not in ["PASS", "-", None]:
            if isinstance(funding_label, str) and funding_label.startswith("-"):
                score += 5  # slightly favor negative funding for shorts
            else:
                score += 2.5

        # 5) Price Spike Stabilizer (20%)
        if price_spike_pct is not None:
            try:
                spike_component = min(abs(float(price_spike_pct)) / 1.0, 1.0)
                score += spike_component * 20
            except Exception:
                pass

        # 6) Price Move (10%)
        if price_pct is not None:
            try:
                price_component = min(abs(float(price_pct)) / 5.0, 1.0)
                score += price_component * 10
            except Exception:
                pass

        # 7) RSI 3m trend alignment (10%)
        if rsi_3m is not None:
            try:
                r3_component = max(0.0, 1 - abs(float(rsi_3m) - 50) / 50)
                score += r3_component * 10
            except Exception:
                pass

        # 8) Orderbook imbalance strength (10%)
        if ob_ratio is not None:
            try:
                r = float(ob_ratio)
                if r <= 0:
                    ob_component = 0.0
                else:
                    ratio_norm = max(r, 1.0 / r)
                    # 1.0 -> 0, 3.0+ -> 1
                    ob_component = min(max((ratio_norm - 1.0) / 2.0, 0.0), 1.0)
                score += ob_component * 10
            except Exception:
                pass

        return int(round(score))

    except Exception:
        return 0


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

        # Open Interest history (15m)
        oi = requests.get(
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

        # Long/Short ratios
        def fetch_ratio(url):
            try:
                d = requests.get(url, timeout=6).json()
                if isinstance(d, list) and len(d) >= 1:
                    return float(d[-1].get("longShortRatio", 50.0))
            except Exception:
                pass
            return "-"

        metrics["acc_r"] = fetch_ratio(f"{base}/topLongShortAccountRatio?symbol={symbol}&period=15m&limit=1")
        metrics["pos_r"] = fetch_ratio(f"{base}/topLongShortPositionRatio?symbol={symbol}&period=15m&limit=1")
        metrics["glb_r"] = fetch_ratio(f"{base}/globalLongShortAccountRatio?symbol={symbol}&period=15m&limit=1")

        # ------------------ FUNDING (current → 15m fallback) ------------------
        try:
            fund = requests.get(
                f"{base}/fundingRate",
                params={"symbol": symbol, "limit": 20},
                timeout=6
            ).json()

            current_f = None
            old_15m = None

            # Latest funding
            try:
                if isinstance(fund, list) and len(fund) >= 1:
                    x = fund[-1].get("fundingRate")
                    if x not in [None, ""]:
                        current_f = float(x)
            except Exception:
                pass

            # Search ~15 minutes earlier
            try:
                if isinstance(fund, list) and len(fund) > 1:
                    ts_now = fund[-1].get("fundingTime", 0)
                    target_min = ts_now - 15*60*1000
                    candidates = [item for item in fund if item.get("fundingTime", 0) <= target_min]
                    if candidates:
                        x2 = candidates[-1].get("fundingRate")
                        if x2 not in [None, ""]:
                            old_15m = float(x2)
            except Exception:
                pass

            # compute funding_change (numeric) if possible
            try:
                if current_f is not None and old_15m is not None:
                    metrics["funding_change"] = (current_f - old_15m) * 100.0
                else:
                    metrics["funding_change"] = 0.0
            except Exception:
                metrics["funding_change"] = 0.0

            # ---- formatting ----
            def interval_map(v):
                v = round(v, 4)
                intervals = [-2, -1, -0.5, -0.1, 0, 0.1, 0.5, 1, 2]
                for i in range(len(intervals)-1):
                    if intervals[i] <= v < intervals[i+1]:
                        return f"{intervals[i]} - {intervals[i+1]}"
                return "< -2" if v < -2 else "> 2"

            # Priority:
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

    # Build text
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

        # weights
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
        score = int(round((raw+1)/2*100))

        if price_pct > 0:
            trend = "LONG" if acc_ratio>55 or pos_ratio>55 else "LONG (weak)" if score>=50 else "LONG (uncertain)"
        else:
            trend = "SHORT" if acc_ratio<45 or pos_ratio<45 else "SHORT (weak)" if score>=50 else "SHORT (uncertain)"

        fake = "HIGH" if oi_change<-3 and notional_change<-3 and abs(price_pct)>=5 else \
               "MEDIUM" if oi_change<0 and notional_change<0 and abs(price_pct)>=3 else \
               "LOW" if score>=60 else "MEDIUM" if score>=40 else "HIGH"
        conf = "HIGH" if score>=80 else "MEDIUM" if score>=60 else "LOW"

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
# TELEGRAM
# ============================================================

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secrets not set.")
        return False
    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": escape_md(text),
            "parse_mode": "MarkdownV2"
        }
        r = requests.post(
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

# ============================================================
# EXIT & REVERSE ENGINE (V3)
# ============================================================

def maybe_send_exit_and_reverse(
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
    Auto EXIT + REVERSE siqnalı.
    START-dan müəyyən vaxt keçəndən sonra periodik olaraq çağırılır.
    """
    if not EXIT_ENABLED:
        return

    start_time = entry.get("start_time")
    if not start_time:
        return

    # EXIT üçün minimum yaş
    if now_ts - start_time < EXIT_MIN_AGE:
        return

    # EXIT check intervalı
    last_check = entry.get("last_exit_check", 0.0)
    if now_ts - last_check < EXIT_CHECK_INTERVAL:
        return
    entry["last_exit_check"] = now_ts

    # Volume drop (1m vs əvvəlki 5m avg)
    vol_drop = 0.0
    if baseline_avg_1m and baseline_avg_1m > 0:
        try:
            vol_drop = max(0.0, 1.0 - (recent_1m / baseline_avg_1m))
        except Exception:
            vol_drop = 0.0

    # Sentiment + RSI + Orderbook + WCE yenilə (cache-lər sayəsində API load azdır)
    closes = get_closes(symbol, limit=100, interval="1m")
    rsi = compute_rsi(closes, RSI_PERIOD)

    sentiment_text, metrics = fetch_sentiment_cached(symbol)
    rsi3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
    ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)
    stage_label = classify_impulse_stage(vol_mult, volume_strength)

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

    # 1) WCE drop
    wce_drop = prev_wce - wce_score
    wce_drop_ok = wce_drop >= EXIT_WCE_DROP
    if wce_drop_ok:
        reasons.append(f"WCE drop {prev_wce:.0f} → {wce_score:.0f}")

    # 2) Volume collapse
    vol_drop_ok = vol_drop >= EXIT_VOLUME_DROP
    if vol_drop_ok:
        reasons.append(f"Volume collapse {vol_drop*100:.1f}%")

    # 3) Micro spike reversal
    micro_reverse_ok = False
    if direction == "LONG" and short_pct <= -EXIT_MICRO_REVERSE:
        micro_reverse_ok = True
        reasons.append(f"Micro reverse {short_pct:.2f}% vs LONG")
    elif direction == "SHORT" and short_pct >= EXIT_MICRO_REVERSE:
        micro_reverse_ok = True
        reasons.append(f"Micro reverse {short_pct:.2f}% vs SHORT")

    # 4) RSI(3m) trend flip
    rsi3_flip_ok = False
    if EXIT_USE_RSI3M_FLIP and prev_rsi3m_trend and rsi3m_trend and rsi3m_trend != prev_rsi3m_trend:
        rsi3_flip_ok = True
        reasons.append(f"RSI(3m) flip {prev_rsi3m_trend} → {rsi3m_trend}")

    # Ən azı 2 səbəb lazımdır
    if len(reasons) < 2:
        entry["last_wce"] = wce_score
        entry["last_trend_dir"] = wce_trend
        entry["last_rsi3m_trend"] = rsi3m_trend
        entry["last_signal_q"] = signal_q
        return

    # EXIT artıq göndərilibsə və çox tezdirsə, təkrar etmə
    last_exit = entry.get("exit_sent_at")
    if last_exit and (now_ts - last_exit) < 60:
        entry["last_wce"] = wce_score
        entry["last_trend_dir"] = wce_trend
        entry["last_rsi3m_trend"] = rsi3m_trend
        entry["last_signal_q"] = signal_q
        return

    vol24 = get_24h_volume_cached(symbol)

    # REVERSE opportunity
    reverse_note = "🔄 Reverse opportunity: NONE"
    reverse_triggered = False
    if REVERSE_ENABLED and direction in ("LONG", "SHORT"):
        reverse_ok = False
        if (
            direction == "LONG"
            and wce_trend.startswith("SHORT")
            and wce_score >= REVERSE_WCE_MIN
            and signal_q >= REVERSE_SIGNALQ_MIN
        ):
            reverse_ok = True
        elif (
            direction == "SHORT"
            and wce_trend.startswith("LONG")
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
        f"Micro spike (short): {short_pct:+.2f}%\n"
        f"RSI(1m): {rsi}\n"
        f"RSI(3m): {rsi3m} ({rsi3m_trend})\n"
        f"Orderbook: {ob_label}\n"
        f"24h Volume: {int(vol24):,} USDT\n"
        f"Signal Quality: {signal_q}/100\n"
        f"WCE Score: {wce_score}%\n\n"
        f"Reasons:\n{reasons_text}\n\n"
        f"{reverse_note}\n"
        f"{wce_text}\n"
        f"{sentiment_text}"
    )

    if send_telegram(caption):
        entry["exit_sent_at"] = now_ts
        entry["tracking"] = False
        entry["stopped_at"] = now_ts
        print(f"{symbol} | EXIT signal sent, tracking stopped.")

        # Log EXIT event
        log_signal("EXIT", {
            "symbol": symbol,
            "direction": direction,
            "pct_15m": pct_15m,
            "short_pct": short_pct,
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

    # Metrikləri yenilə
    entry["last_wce"] = wce_score
    entry["last_trend_dir"] = wce_trend
    entry["last_rsi3m_trend"] = rsi3m_trend
    entry["last_signal_q"] = signal_q

# ============================================================
# WEBSOCKET CORE: _process_mini + handle_miniticker
# ============================================================

ws_manager = None  # active ThreadedWebsocketManager instance

def get_score_level(score: int) -> str:
    """
    START / STEP bildirişlərində istifadə olunan eyni level mapping.
    """
    if score < 40:
        return "WEAK ⚠️"
    if score < 70:
        return "GOOD 👍"
    if score < 90:
        return "STRONG 🔥"
    return "ULTRA 🚀"


def _process_mini(msg):
    """
    Binance miniticker mesajı üçün core START / STEP / EXIT məntiqi.
    """
    global last_log, last_any_msg_ts, last_start_ts

    symbol = msg.get("s") or msg.get("symbol")
    if not symbol:
        return

    now = time.time()
    last_any_msg_ts = now  # hər gələn miniticker-də yenilə

    # ----------- LOG LIMIT (console izləmə) -----------
    if now - last_log > 2:
        try:
            price_raw = msg.get("c", msg.get("lastPrice", 0))
            open_raw  = msg.get("o", msg.get("openPrice", 0))
            vol_raw   = msg.get("v", msg.get("volume", 0))

            try:
                price = float(price_raw)
            except Exception:
                price = 0.0

            try:
                open_ = float(open_raw)
            except Exception:
                open_ = 0.0

            try:
                vol = float(vol_raw)
            except Exception:
                vol = 0.0

            direction = "🔺" if price >= open_ else "🔻"
            vol_display = f"{vol:,.0f}" if vol > 0 else "0"
            print(f"{symbol:<12} {price:>10.4f} {direction} | vol {vol_display}")
        except Exception:
            print("Mini data: <parse error>")

        last_log = now
    # --------------------------------------------------

    try:
        # yalnız USDT cütlərini qəbul et
        if not symbol.endswith("USDT"):
            return

        if tracked_syms and symbol not in tracked_syms:
            return

        price_raw = msg.get("c", msg.get("lastPrice", 0))
        vol_raw = msg.get("v", msg.get("volume", 0))

        try:
            price = float(price_raw)
        except Exception:
            return

        try:
            vol = float(vol_raw)
        except Exception:
            vol = 0.0

        if price == 0:
            return

        # --- per-symbol tracking state ---
        entry = state.setdefault(symbol, {
            "prices": [],
            "vols": [],
            "last_v": None,
            "tracking": False,
            "start_price": None,
            "last_step_price": None,
            "last_step_time": None,
            "stopped_at": None,
            # V3 fields
            "start_time": None,
            "exit_sent_at": None,
            "last_exit_check": 0.0,
            "direction": None,
            "last_wce": None,
            "last_trend_dir": None,
            "last_rsi3m_trend": None,
            "last_signal_q": None,
        })

        entry["prices"].append(price)

        if entry["last_v"] is None:
            diff_vol = 0.0
        else:
            diff_vol = max(vol - entry["last_v"], 0.0)
        entry["last_v"] = vol
        entry["vols"].append(diff_vol)

        # max 30 dəq history (təxminən 1 sample/s → 1800 sample)
        if len(entry["prices"]) > 1800:
            entry["prices"] = entry["prices"][-1800:]
            entry["vols"] = entry["vols"][-1800:]

        last_seen[symbol] = now

        if len(entry["prices"]) < 5:
            return

        # --- 15 dəqiqəlik lookback qiyməti ---
        lookback_samples = LOOKBACK_MIN * 60
        if len(entry["prices"]) >= lookback_samples:
            price_15min_ago = entry["prices"][-lookback_samples]
        else:
            price_15min_ago = entry["prices"][0]

        pct_15m = (price - price_15min_ago) / price_15min_ago * 100 if price_15min_ago else 0.0

        # ---------- VOLUME SPIKE (1m vs previous 5m avg) ----------
        vols = entry["vols"]
        n_vols = len(vols)
        recent_1m = 0.0
        baseline_avg_1m = 0.0

        if n_vols >= 360:  # 6 dəq history (1m + 5m)
            recent_1m = sum(vols[-60:])
            prev_5m = sum(vols[-360:-60])
            baseline_avg_1m = prev_5m / 5.0 if prev_5m > 0 else 0.0
        elif n_vols >= 120:
            recent_1m = sum(vols[-60:])
            prev_5m = sum(vols[-120:-60])
            baseline_avg_1m = prev_5m if prev_5m > 0 else 0.0

        if baseline_avg_1m > 0:
            vol_mult = recent_1m / baseline_avg_1m
        else:
            vol_mult = 1.0

        recent_sum = recent_1m

        # ---------- PRICE SPIKE STABILIZER (short window) ----------
        short_pct = 0.0
        if len(entry["prices"]) >= SHORT_WINDOW:
            try:
                base_short = entry["prices"][-SHORT_WINDOW]
                short_pct = (price - base_short) / base_short * 100 if base_short else 0.0
            except Exception:
                short_pct = 0.0

        if recent_sum < MIN_RECENT_VOLUME_USDT:
            fake_tag = " FAKE"
        else:
            fake_tag = ""

        if vol_mult > 1000:
            vol_mult_display = f">1000×{fake_tag}"
        else:
            vol_mult_display = f"×{vol_mult:.2f}{fake_tag}"

        # ---- VOLUME STRENGTH (son 15m / əvvəlki 15m) ----
        last_15_volume = 0.0
        prev_15_volume = 0.0
        if len(vols) >= 1800:
            last_15_volume = sum(vols[-900:])
            prev_15_volume = sum(vols[-1800:-900])
        elif len(vols) >= 900:
            last_15_volume = sum(vols[-900:])
            prev_15_volume = sum(vols[:-900]) if len(vols) > 900 else 0.0

        if prev_15_volume > 0:
            volume_strength = last_15_volume / prev_15_volume
        else:
            volume_strength = 0.0

        START_PCT = THRESHOLD
        STOP_SECONDS = 2 * 3600
        now_ts = now

        # ================= TRACKING MODE =================
        if entry.get("tracking"):
            last_step_time = entry.get("last_step_time") or now_ts
            if now_ts - last_step_time >= STOP_SECONDS:
                entry["tracking"] = False
                entry["stopped_at"] = now_ts
                print(f"{symbol} | tracking stopped due to inactivity ({STOP_SECONDS}s) — PASSIVE now.")
                return

            # STEP trigger
            last_step_price = entry.get("last_step_price", price)
            pct_from_last_step = ((price - last_step_price) / last_step_price) * 100 if last_step_price else 0.0

            step_fired = False
            if (
                abs(pct_from_last_step) >= STEP_PCT and
                vol_mult >= STEP_VOLUME_SPIKE and
                volume_strength >= STEP_VOLUME_STRENGTH
            ):
                start_price = entry.get("start_price", last_step_price)
                pct_from_start = ((price - start_price) / start_price) * 100 if start_price else 0.0

                entry["last_step_price"] = price
                entry["last_step_time"] = now_ts

                closes = get_closes(symbol, limit=100, interval="1m")
                rsi = compute_rsi(closes, RSI_PERIOD)

                sentiment_text, metrics = fetch_sentiment_cached(symbol)
                rsi3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
                ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)
                stage_label = classify_impulse_stage(vol_mult, volume_strength)

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

                vol24 = get_24h_volume_cached(symbol)

                caption = (
                    "⚡ STEP\n"
                    f"{symbol}\n\n"
                    f"📈 Change (15m): {pct_15m:+.2f}% "
                    f"{'(UP 🔺)' if pct_15m>0 else '(DOWN 🔻)'}\n"
                    f"💰 Price: {price}\n"
                    f"🔎 Δ from START: {pct_from_start:+.2f}% (start_price: {start_price})\n"
                    f"📊 Volume spike (1m/5m): {vol_mult_display}\n"
                    f"💪 Volume Strength (15m/15m): {volume_strength:.2f}x\n"
                    f"⚡ Micro Spike (short): {short_pct:+.2f}%\n"
                    f"⏳ Impulse Stage: {stage_label}\n"
                    f"📦 24h Volume: {int(vol24):,} USDT\n"
                    f"📉 RSI(1m): {rsi}\n"
                    f"📉 RSI(3m): {rsi3m} ({rsi3m_trend})\n"
                    f"📊 Orderbook: {ob_label}\n"
                    f"{sentiment_text}\n"
                    f"🔍 Signal Quality: {signal_q}/100 ({get_score_level(signal_q)})\n\n"
                    f"{wce_text}"
                )

                if send_telegram(caption):
                    notified[symbol] = now_ts
                    entry["last_wce"] = wce_score
                    entry["last_trend_dir"] = wce_trend
                    entry["last_rsi3m_trend"] = rsi3m_trend
                    entry["last_signal_q"] = signal_q

                    # Log STEP event
                    log_signal("STEP", {
                        "symbol": symbol,
                        "price": price,
                        "pct_15m": pct_15m,
                        "pct_from_start": pct_from_start,
                        "pct_from_last_step": pct_from_last_step,
                        "short_pct": short_pct,
                        "volume_strength": volume_strength,
                        "vol_mult": vol_mult,
                        "wce_score": wce_score,
                        "signal_q": signal_q,
                        "stage": stage_label,
                    })

                step_fired = True

            # STEP olmasa EXIT engine-i işə sal
            if not step_fired:
                maybe_send_exit_and_reverse(
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
                )
            return

        # ================= PASSIVE MODE (START axtarır) =================
        else:
            vol24 = get_24h_volume_cached(symbol)
            if (
                abs(pct_15m) >= START_PCT
                and vol_mult >= VOLUME_SPIKE
                and volume_strength >= VOLUME_STRENGTH_MIN
                and vol24 >= MIN24H
                and abs(short_pct) >= PRICE_SPIKE_MIN
            ):
                entry["tracking"] = True
                entry["start_price"] = price
                entry["last_step_price"] = price
                entry["last_step_time"] = now_ts
                entry["stopped_at"] = None

                # V3 fields
                entry["start_time"] = now_ts
                entry["exit_sent_at"] = None
                entry["last_exit_check"] = 0.0
                entry["direction"] = "LONG" if pct_15m > 0 else "SHORT"

                closes = get_closes(symbol, limit=100, interval="1m")
                rsi = compute_rsi(closes, RSI_PERIOD)

                sentiment_text, metrics = fetch_sentiment_cached(symbol)
                rsi3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
                ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)
                stage_label = classify_impulse_stage(vol_mult, volume_strength)

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

                caption = (
                    "🚀 START\n"
                    f"{symbol}\n\n"
                    f"📈 Change (15m): {pct_15m:+.2f}% "
                    f"{'(UP 🔺)' if pct_15m>0 else '(DOWN 🔻)'}\n"
                    f"💰 Price: {price}\n"
                    f"📊 Volume spike (1m/5m): {vol_mult_display}\n"
                    f"💪 Volume Strength (15m/15m): {volume_strength:.2f}x\n"
                    f"⚡ Micro Spike (short): {short_pct:+.2f}%\n"
                    f"⏳ Impulse Stage: {stage_label}\n"
                    f"📦 24h Volume: {int(vol24):,} USDT\n"
                    f"📉 RSI(1m): {rsi}\n"
                    f"📉 RSI(3m): {rsi3m} ({rsi3m_trend})\n"
                    f"📊 Orderbook: {ob_label}\n"
                    f"{sentiment_text}\n"
                    f"🔍 Signal Quality: {signal_q}/100 ({get_score_level(signal_q)})\n\n"
                    f"{wce_text}"
                )

                if send_telegram(caption):
                    notified[symbol] = now_ts
                    entry["last_wce"] = wce_score
                    entry["last_trend_dir"] = wce_trend
                    entry["last_rsi3m_trend"] = rsi3m_trend
                    entry["last_signal_q"] = signal_q
                    last_start_ts = now_ts

                    # Log START event
                    log_signal("START", {
                        "symbol": symbol,
                        "price": price,
                        "pct_15m": pct_15m,
                        "short_pct": short_pct,
                        "volume_strength": volume_strength,
                        "vol_mult": vol_mult,
                        "wce_score": wce_score,
                        "signal_q": signal_q,
                        "direction": entry["direction"],
                        "stage": stage_label,
                    })

            return

    except Exception as e:
        try:
            short = repr(msg)[:300]
        except Exception:
            short = "<unprintable>"
        print("process_mini error:", e, "| msg:", short)


def handle_miniticker(msg):
    """
    Accept either a dict (single symbol) or a list of dicts (bulk).
    """
    try:
        if msg is None:
            return

        if isinstance(msg, (list, tuple)):
            for item in msg:
                if isinstance(item, dict):
                    _process_mini(item)
        elif isinstance(msg, dict):
            _process_mini(msg)
        else:
            print("handle_miniticker: unexpected msg type", type(msg))

    except Exception as e:
        print("handle_miniticker wrapper error:", e)

# ============================================================
# WEBSOCKET MONITOR — Railway Stable Version
# ============================================================

def ws_monitor(min_active=10, check_interval=30):
    """
    Railway üçün sabit WebSocket monitoru.
    Sadə: əgər 90 saniyədən çoxdur symbol görülmürsə → reconnect.
    """
    global ws_manager

    while True:
        try:
            now = time.time()

            active = sum(1 for s in tracked_syms if last_seen.get(s, 0) > now - 90)

            if active < min_active:
                print(f"⚠ WS monitor: Only {active} active symbols — reconnecting WS...")

                try:
                    if ws_manager is not None:
                        ws_manager.stop()
                        time.sleep(2)
                except:
                    pass

                try:
                    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
                    twm.start()
                    ws_manager = twm
                    twm.start_miniticker_socket(callback=handle_miniticker)
                    print("🔁 WebSocket reconnected successfully.")
                except Exception as e:
                    print("❌ WS reconnect failed:", e)

            time.sleep(check_interval)

        except Exception as e:
            print("ws_monitor error:", e)
            time.sleep(check_interval)

# ============================================================
# HEARTBEAT THREAD — Alive ping to Telegram
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

                hb_text = (
                    "🤖 Scanner Alive (Railway)\n"
                    f"Tracked pairs: {total}\n"
                    f"Active (last 90s): {active}\n"
                    f"Uptime: {format_uptime(uptime)}\n"
                    f"WS: {ws_status}\n"
                    f"Last tick age: {int(last_msg_age)}s" if last_msg_age is not None else "Last tick age: N/A"
                )

                send_telegram(hb_text)
                last_heartbeat_ts = now

            time.sleep(15)
        except Exception as e:
            print("heartbeat_loop error:", e)
            time.sleep(30)

# ============================================================
# WATCHDOG — auto-restart if no data
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
# START STREAM — stable mode for Railway
# ============================================================

def start_stream():
    global ws_manager, tracked_syms

    print("🔍 Loading symbols...")

    try:
        info = client.futures_exchange_info()
        syms = [s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]
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

    try:
        twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
        twm.start()
        ws_manager = twm

        # Single global stream — Railway üçün ən stabil variant
        twm.start_miniticker_socket(callback=handle_miniticker)
        print("🔌 Subscribed to MINITICKER global stream.")

    except Exception as e:
        print("❌ Failed to start global miniticker socket:", e)
        return

    # Background monitor
    threading.Thread(target=ws_monitor, daemon=True).start()
    print("🚀 Scanner started (WebSocket + Monitor)")

# ============================================================
# HEARTBEAT SYSTEM — Telegram Alive Ping (Railway safe)
# ============================================================

def heartbeat(interval_minutes=30):
    start_ts = time.time()

    while True:
        try:
            uptime_h = (time.time() - start_ts) / 3600
            active_syms = sum(1 for s in tracked_syms if last_seen.get(s, 0) > time.time() - 120)

            ws_status = "OK" if active_syms >= 5 else "WEAK ⚠️"

            text = (
                "🤖 *Scanner Alive (Railway)*\n"
                f"⏳ Uptime: {uptime_h:.1f}h\n"
                f"📡 Active Symbols: {active_syms}/{len(tracked_syms)}\n"
                f"🔌 WebSocket: {ws_status}\n"
                f"🕒 Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            send_telegram(text)

        except Exception as e:
            print("Heartbeat error:", e)

        time.sleep(interval_minutes * 60)

# ============================================================
# MAIN (Railway Optimized)
# ============================================================

if __name__ == "__main__":
    print("\n📡 SCANNER STARTING (Railway Mode)...\n")

    try:
        threading.Thread(target=start_stream, daemon=True).start()
        threading.Thread(target=heartbeat_loop, daemon=True).start()
        threading.Thread(target=watchdog_loop, daemon=True).start()
    except Exception as e:
        print("❌ Failed to start background threads:", e)

    # Railway-də main thread boş qalmamalıdır (container yoxsa ölür)
    while True:
        time.sleep(5)
