# ============================================================
#  main.py — PRO QUANT MAIN PROCESSOR (FINAL, STABLE)
#  Base: Paket B logic
#  Pattern + Spot + Signal Quality integrated (SAFE)
# ============================================================

import asyncio
import time
from binance.client import Client

from config import (
    SYMBOL_LIMIT,
    MIN_24H_VOL,
    START_PCT,
    RECENT_1M_MIN,
)

from ws_engine import start_ws_engine, register_tick_callback

from pro_core import (
    get_24h_volume_cached,
    fetch_rsi_3m_cached,
    fetch_orderbook_imbalance_cached,
    compute_signal_quality,
    compute_wce,
    send_telegram,
    log_signal,
)

from sentiment_engine import (
    fetch_sentiment_cached,
    format_sentiment_text,
)

from pattern_engine import generate_pattern_analysis


# ============================================================
# IN-MEMORY BUFFERS
# ============================================================

price_history = {}
volume_history = {}


def add_price(symbol, price):
    arr = price_history.setdefault(symbol, [])
    arr.append(price)
    if len(arr) > 3600:
        arr.pop(0)


def add_volume(symbol, vol):
    arr = volume_history.setdefault(symbol, [])
    arr.append(vol)
    if len(arr) > 3600:
        arr.pop(0)


def compute_price_change_15m(symbol):
    arr = price_history.get(symbol, [])
    if len(arr) < 900:
        return None
    old = arr[-900]
    now = arr[-1]
    if old <= 0:
        return None
    return (now - old) / old * 100


def compute_volume_metrics(symbol):
    vols = volume_history.get(symbol, [])
    if len(vols) < 1000:
        return None, None, None

    recent_1m = sum(vols[-60:])
    prev_5m = sum(vols[-360:-60])
    baseline = prev_5m / 5 if prev_5m > 0 else 0.0
    vol_mult = recent_1m / baseline if baseline > 0 else 0.0

    last_15 = sum(vols[-900:])
    prev_15 = sum(vols[-1800:-900])
    volume_strength = (last_15 / prev_15) if prev_15 > 0 else 0.0

    return recent_1m, vol_mult, volume_strength


# ============================================================
# WS TICK HANDLER (SAFE, EVENT-DRIVEN)
# ============================================================

def on_tick(symbol, price, vol):
    add_price(symbol, price)
    add_volume(symbol, vol)

    if len(price_history.get(symbol, [])) < 900:
        return

    recent_1m, vol_mult, volume_strength = compute_volume_metrics(symbol)
    if recent_1m is None:
        return

    if recent_1m < RECENT_1M_MIN:
        return
    if vol_mult < 3:
        return

    pct_15m = compute_price_change_15m(symbol)
    if pct_15m is None or abs(pct_15m) < START_PCT:
        return

    # --- TECHNICAL CONTEXT ---
    rsi_3m, rsi3m_trend = fetch_rsi_3m_cached(symbol)
    ob_ratio, ob_label = fetch_orderbook_imbalance_cached(symbol)

    # --- SENTIMENT (DICT ONLY) ---
    metrics = fetch_sentiment_cached(symbol)
    sentiment_text = format_sentiment_text(metrics)

    # --- WCE ---
    wce_score, wce_trend, wce_fake, wce_conf, wce_text = compute_wce(
        metrics.get("oi_chg"),
        metrics.get("not_chg"),
        metrics.get("acc_r"),
        metrics.get("pos_r"),
        metrics.get("glb_r"),
        metrics.get("funding_change"),
        rsi=None,                     # RSI(1m) yoxdur bu mərhələdə
        price_pct=pct_15m,
        rsi_3m=rsi_3m,
        ob_ratio=ob_ratio,
    )

    # --- SIGNAL QUALITY ---
    signal_q = compute_signal_quality(
        rsi=None,
        vol_mult=vol_mult,
        oi_chg=metrics.get("oi_chg"),
        funding_label=metrics.get("last_f"),
        price_spike_pct=None,
        price_pct=pct_15m,
        rsi_3m=rsi_3m,
        ob_ratio=ob_ratio,
    )

    # --- PATTERN SUMMARY (SAFE OBSERVER) ---
    pattern_block = generate_pattern_analysis(
        metrics,
        pct_15m,
        rsi=None,
        rsi3m=rsi_3m,
        ob_ratio=ob_ratio,
        ob_label=ob_label,
        stage=None,
        mode="full",
    )

    # --- TELEGRAM MESSAGE ---
    text = (
        "🚀 START SIGNAL\n"
        f"{symbol}\n\n"
        f"📈 15m Change: {pct_15m:+.2f}%\n"
        f"📊 Volume Spike: x{vol_mult:.2f}\n"
        f"💪 Volume Strength: x{volume_strength:.2f}\n"
        f"🔍 Signal Quality: {signal_q}/100\n\n"
        f"{wce_text}\n"
        f"{pattern_block}\n"
        f"{sentiment_text}"
    )

    send_telegram(text)
    log_signal("START", {
        "symbol": symbol,
        "pct_15m": pct_15m,
        "vol_mult": vol_mult,
        "signal_q": signal_q,
        "wce": wce_score,
    })


# ============================================================
# MAIN LOOP
# ============================================================

async def run():
    print("🚀 PRO QUANT scanner starting...")

    client = Client("", "")
    info = client.futures_exchange_info()
    syms = [s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT"]

    tracked = []
    for s in syms[:SYMBOL_LIMIT]:
        v = get_24h_volume_cached(s)
        if v >= MIN_24H_VOL:
            tracked.append(s)

    print("Tracking symbols:", tracked)

    register_tick_callback(on_tick)
    await start_ws_engine()

    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run())
