# ============================================================
# config.py — PRO QUANT GLOBAL CONFIG (FINAL)
# Base: Paket B (Stable)
# ============================================================

import os

# ============================================================
# TELEGRAM
# ============================================================

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# BINANCE
# ============================================================

FAPI_BASE = "https://fapi.binance.com"

# ============================================================
# SYMBOL SELECTION
# ============================================================

SYMBOL_LIMIT = 50              # TOP N USDT futures
MIN_24H_VOL  = 2_000_000       # minimum 24h quote volume (USDT)

# ============================================================
# CACHE TTL (seconds)
# ============================================================

RSI_CACHE_TTL        = 20
ORDERBOOK_CACHE_TTL  = 10
SENTIMENT_CACHE_TTL  = 30
VOL24_CACHE_TTL      = 300

# ============================================================
# CORE WINDOWS
# ============================================================

LOOKBACK_MIN   = 15             # 15m impulse window
SHORT_WINDOW   = 5              # micro spike window (ticks)

# ============================================================
# START LOGIC (PRIMARY TRIGGER)
# ============================================================

START_PCT            = 5.0      # 15m price change %
START_VOLUME_SPIKE   = 3.0      # 1m vs 5m volume multiple
START_MICRO_PCT      = 0.05     # micro impulse %

RECENT_1M_MIN        = 2000     # minimum recent 1m volume (USDT)

FAKE_VOLUME_STRENGTH = 1.5      # last 15m / prev 15m volume
FAKE_RECENT_MIN_USDT = 2000     # weak recent volume filter
FAKE_RECENT_STRONG   = 10000    # override fake filter if strong

# ============================================================
# STEP LOGIC (CONTINUATION)
# ============================================================

STEP_PCT              = 5.0
STEP_VOLUME_SPIKE     = 2.0
STEP_VOLUME_STRENGTH  = 1.2

# ============================================================
# EXIT / REVERSE (USED LATER)
# ============================================================

EXIT_MIN_AGE        = 240       # seconds after START
EXIT_CHECK_INTERVAL = 20
EXIT_WCE_DROP       = 25.0
EXIT_VOLUME_DROP    = 0.55
EXIT_MICRO_REVERSE  = 0.15

REVERSE_WCE_MIN     = 60
REVERSE_SIGNALQ_MIN = 60
