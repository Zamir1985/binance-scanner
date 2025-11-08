# scanner_websocket.py
# Real-time Binance Futures scanner with mini-chart, open interest, funding rate, RSI
# Paste this entire file content into your scanner_websocket.py and run: python3 scanner_websocket.py

import os
import time
import json
import math
import requests
import threading
import numpy as np
import matplotlib
matplotlib.use("Agg")   # use non-interactive backend for servers
import matplotlib.pyplot as plt
from io import BytesIO
from binance import ThreadedWebsocketManager
from binance.client import Client

# Optional keep_alive (Replit auto-sleep prevention)
try:
    from keep_alive import keep_alive
    threading.Thread(target=keep_alive, daemon=True).start()
    print("🌐 keep_alive started ✅ (background thread)")
except Exception as e:
    print("⚠️ keep_alive not loaded:", e)

# === SETTINGS ===
THRESHOLD = 7.0            # % change (up/down) to alert
VOLUME_SPIKE = 3.0         # recent volume must be >= avg_prev * this
LOOKBACK_MIN = 15          # minutes to compare for % move and RSI
RSI_PERIOD = 14            # RSI period
MIN24H = 1_000_000         # minimum 24h quoteVolume (USDT) -- you asked for 1m
NOTIFY_COOLDOWN = 30 * 60  # seconds between alerts per symbol
TOP_N = 100                # top N symbols by 24h volume to track
# =================

# === API KEYS ===
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
notified = {}

# Binance REST endpoints (futures)
FUTURES_TICKER_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FUTURES_KLINES = "https://fapi.binance.com/fapi/v1/klines"
FUTURES_OPEN_INTEREST = "https://fapi.binance.com/fapi/v1/openInterest"
FUTURES_FUNDING_RATE = "https://fapi.binance.com/fapi/v1/fundingRate"

# ----------------------------------------
# Utilities: market data (REST, fallback)
# ----------------------------------------
def get_24h_volume(symbol):
    try:
        r = requests.get(FUTURES_TICKER_24H, params={"symbol": symbol}, timeout=15)
        data = r.json()
        return float(data.get("quoteVolume", 0))
    except Exception:
        return 0.0

def get_open_interest(symbol):
    try:
        r = requests.get(FUTURES_OPEN_INTEREST, params={"symbol": symbol}, timeout=6)
        data = r.json()
        return float(data.get("openInterest", 0))
    except Exception:
        return 0.0

def get_latest_funding(symbol):
    try:
        # fundingRate returns list; limit=1 gives last funding entry
        r = requests.get(FUTURES_FUNDING_RATE, params={"symbol": symbol, "limit": 1}, timeout=6)
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            return float(data[0].get("fundingRate", 0)), data[0].get("fundingTime")
        return 0.0, None
    except Exception:
        return 0.0, None

def get_klines_close(symbol, minutes, interval='1m'):
    """Return list of close prices for last `minutes` minutes (limit=minutes+1)."""
    try:
        limit = max(minutes + 5, RSI_PERIOD + 5)
        r = requests.get(FUTURES_KLINES, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=8)
        data = r.json()
        if not isinstance(data, list):
            return []
        closes = [float(k[4]) for k in data]
        return closes
    except Exception:
        return []

# ----------------------------------------
# RSI calculation
# ----------------------------------------
def compute_rsi(prices, period=14):
    if not prices or len(prices) < period + 1:
        return None
    deltas = np.diff(np.array(prices))
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        rs = float('inf')
        rsi = 100.0
    else:
        rs = up / down
        rsi = 100.0 - (100.0 / (1.0 + rs))
    # candlestick style exponential smoothing
    up_avg = up
    down_avg = down
    for i in range(period, len(deltas)):
        delta = deltas[i]
        up_avg = (up_avg * (period - 1) + max(delta, 0)) / period
        down_avg = (down_avg * (period - 1) + max(-delta, 0)) / period
    if down_avg == 0:
        return 100.0
    rs = up_avg / down_avg
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(float(rsi), 2)

# ----------------------------------------
# mini chart generator (returns bytes)
# ----------------------------------------
def make_mini_chart(symbol, prices):
    try:
        if not prices or len(prices) < 2:
            return None
        buf = BytesIO()
        plt.figure(figsize=(4,2.2), dpi=100)
        plt.plot(prices, linewidth=1)
        plt.fill_between(range(len(prices)), prices, alpha=0.05)
        plt.title(symbol, fontsize=8)
        plt.xticks([], [])
        plt.yticks([], [])
        plt.grid(False)
        plt.tight_layout()
        plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0)
        plt.close()
        buf.seek(0)
        return buf
    except Exception as e:
        print("chart error:", e)
        return None

# ----------------------------------------
# Telegram: choose sendPhoto with caption (better than text-only)
# ----------------------------------------
def send_telegram_with_chart(symbol, caption, chart_buf):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secrets not set.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        files = {}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        if chart_buf:
            files['photo'] = ('chart.png', chart_buf.getvalue())
        else:
            # If no chart, fallback to text message
            return send_telegram_text(caption)
        r = requests.post(url, data=data, files=files, timeout=15)
        if r.ok:
            print("📩 Telegram photo sent:", symbol)
            return True
        else:
            print("❌ Telegram photo failed:", r.status_code, r.text)
            return send_telegram_text(caption)
    except Exception as e:
        print("Telegram send error:", e)
        return False

def send_telegram_text(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secrets not set.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        if r.ok:
            print("📩 Telegram text sent")
            return True
        print("❌ Telegram text failed:", r.status_code, r.text)
        return False
    except Exception as e:
        print("Telegram send error:", e)
        return False

# ----------------------------------------
# state / websocket handler
# ----------------------------------------
state = {}      # symbol -> {"prices": [...], "vols": [...], "last_time": ts}
def handle_miniticker(msg):
    """
    msg example from futures miniTicker websocket:
    {
      "e":"24hrMiniTicker",
      "E":123456789,
      "s":"BTCUSDT",
      "c":"0.0025",    # close price
      "o":"0.0010",
      "h":"0.0025",
      "l":"0.0010",
      "v":"10000"     # volume (base asset)
    }
    but different websockets use slightly different fields; this handler checks keys.
    """
    try:
        # Accept msg with symbol & close price & volume (various field names)
        symbol = msg.get("s") or msg.get("symbol")
        price_str = msg.get("c") or msg.get("price") or msg.get("p")
        vol_str = msg.get("v") or msg.get("q") or msg.get("volume")
        if not symbol or not price_str:
            return
        price = float(price_str)
        vol = float(vol_str) if vol_str is not None else 0.0

        # track state (per-second resolution approximate)
        now = time.time()
        entry = state.setdefault(symbol, {"prices": [], "vols": [], "times": []})
        entry["prices"].append(price)
        entry["vols"].append(vol)
        entry["times"].append(now)

        # keep about LOOKBACK_MIN * 60 points (cull older)
        maxlen = max(LOOKBACK_MIN * 60, 60)
        if len(entry["prices"]) > maxlen:
            entry["prices"] = entry["prices"][-maxlen:]
            entry["vols"] = entry["vols"][-maxlen:]
            entry["times"] = entry["times"][-maxlen:]

        # Quick checks using last and earliest in buffer
        if len(entry["prices"]) < 5:
            return

        old_price = entry["prices"][0]
        pct = (price - old_price) / old_price * 100 if old_price else 0.0

        # Recent volume: average of last 10 values vs previous window average
        recent_window = min(30, len(entry["vols"]))
        recent_avg = max(1e-9, sum(entry["vols"][-recent_window:]) / recent_window)
        prev_window = max(recent_window, 60)
        prev_avg = max(1e-9, sum(entry["vols"][-prev_window:-recent_window]) / max(1, prev_window - recent_window)) if len(entry["vols"]) > prev_window else recent_avg

        vol_mult = recent_avg / prev_avg if prev_avg else 1.0

        # Additional checks: 24h quote volume, open interest, funding rate, RSI
        vol24 = get_24h_volume(symbol)
        oi = get_open_interest(symbol)
        fund_rate, fund_time = get_latest_funding(symbol)

        closes = get_klines_close(symbol, max(LOOKBACK_MIN, RSI_PERIOD + 1))
        rsi = compute_rsi(closes[-(RSI_PERIOD+1):], RSI_PERIOD) if closes else None

        # Print to console for debugging
        print(f"{symbol} | Δ {pct:+.2f}% | vol×{vol_mult:.2f} | 24h:{int(vol24):,} | OI:{int(oi):,} | RSI:{rsi}")

        # Filter conditions to send alert:
        #  - percent change ≥ THRESHOLD (abs)
        #  - recent volume spike ≥ VOLUME_SPIKE
        #  - 24h volume ≥ MIN24H
        # (You can tune these conditions below)
        if abs(pct) >= THRESHOLD and vol_mult >= VOLUME_SPIKE and vol24 >= MIN24H:
            now_ts = time.time()
            last = notified.get(symbol, 0)
            if now_ts - last < NOTIFY_COOLDOWN:
                return

            # prepare message (HTML parse_mode)
            direction = "🔺 UP" if pct > 0 else "🔻 DOWN"
            time_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now_ts))
            caption = (
                f"🚨 <b>{symbol}</b>\n"
                f"📈 <b>Change:</b> {pct:+.2f}% ({direction})\n"
                f"💰 <b>Price:</b> {price:,.6f} USDT\n"
                f"📊 <b>Volume spike:</b> ×{vol_mult:.2f}\n"
                f"🕒 <b>Time:</b> {time_str}\n"
                f"📦 <b>24h Volume:</b> {int(vol24):,} USDT\n"
                f"🔗 <b>Open Interest:</b> {int(oi):,}\n"
                f"⚖️ <b>Funding:</b> {fund_rate:+.8f}\n"
            )
            # Make mini-chart from recent closes if possible
            chart_buf = None
            if len(entry["prices"]) >= 8:
                # use last N closes
                chart_prices = entry["prices"][-min(len(entry["prices"]), 120):]
                chart_buf = make_mini_chart(symbol, chart_prices)

            ok = send_telegram_with_chart(symbol, caption, chart_buf)
            if ok:
                notified[symbol] = now_ts
            else:
                print("Telegram send failed for", symbol)

    except Exception as e:
        print("handle_miniticker error:", e)

# ----------------------------------------
# start websocket manager and subscribe to miniTicker for top symbols
# ----------------------------------------
def start_stream():
    try:
        info = client.futures_exchange_info()
        syms = [s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]
        # sort by 24h volume and take top N
        vol_list = [(s, get_24h_volume(s)) for s in syms]
        vol_list = [(s, v) for s, v in vol_list if v >= MIN24H]
        vol_list.sort(key=lambda x: x[1], reverse=True)
        top_syms = [s for s, v in vol_list[:TOP_N]]
        print(f"✅ Futures bazarından {len(syms)} USDT cüt tapıldı. Top {len(top_syms)} selected.")
    except Exception as e:
        print("symbol load error:", e)
        top_syms = []

    if not top_syms:
        print("No symbols to track (try lowering MIN24H). Exiting.")
        return

    # Start websocket manager
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()
    print(f"✅ Tracking {len(top_syms)} futures symbols via miniTicker WebSocket...")

    # subscribe symbol miniTicker sockets
    for s in top_syms:
        try:
            # For python-binance ThreadedWebsocketManager, easiest is to open a combined stream (many symbols)
            twm.start_symbol_miniticker_socket(callback=handle_miniticker, symbol=s)
            time.sleep(0.01)
        except Exception as e:
            print("subscribe error for", s, e)

    print("🚀 WebSocket scanner started... (real-time updates)")

# ----------------------------------------
# main
# ----------------------------------------
if __name__ == "__main__":
    start_stream()
    while True:
        time.sleep(1)
