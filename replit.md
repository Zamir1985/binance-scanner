# Binance Scanner - Telegram Notifier

## Overview
This is a Python-based cryptocurrency scanner that monitors Binance spot and futures markets for significant price movements and volume spikes, sending real-time notifications via Telegram.

**Current State**: Fully configured and ready to run. The scanner continuously monitors top cryptocurrency pairs and alerts when specific conditions are met.

## Recent Changes
- **November 2, 2025**: 
  - Initial import from GitHub
  - Migrated hardcoded API keys to environment variables for security
  - Set up Replit environment with Python 3.11 and all dependencies
  - Configured workflow for continuous scanning

## Project Architecture

### Main Components
- **scanner_telegram.py**: Main scanner application that:
  - Fetches top cryptocurrency pairs by 24h volume from Binance
  - Monitors price changes over configurable time windows
  - Detects volume spikes compared to historical averages
  - Sends Telegram notifications when alert criteria are met
  - Implements cooldown periods to avoid spam

### Dependencies
- `requests`: HTTP library for Telegram API calls
- `python-binance`: Official Binance API client
- `pyTelegramBotAPI`: Telegram Bot API wrapper

### Configuration Parameters
The scanner behavior is controlled by constants in `scanner_telegram.py`:
- **THRESHOLD**: Minimum price change percentage to trigger alert (default: 5%)
- **LOOKBACK_MINUTES**: Time window for measuring price change (default: 15 min)
- **AVG_WINDOW_MINUTES**: Historical window for volume comparison (default: 60 min)
- **VOLUME_SPIKE_MULTIPLIER**: Required volume spike multiplier (default: 3.0x)
- **MIN_24H_VOLUME_USDT**: Minimum 24h volume filter (default: 3M USDT)
- **SCAN_INTERVAL**: Seconds between scans (default: 20 sec)
- **COOLDOWN_MINUTES**: Cooldown before re-alerting same symbol (default: 30 min)
- **TOP_N**: Number of top volume pairs to monitor (default: 100)

## Required Environment Variables

Before running the scanner, you must set these environment variables:

1. **BINANCE_API_KEY**: Your Binance API key
2. **BINANCE_API_SECRET**: Your Binance API secret
3. **TELEGRAM_BOT_TOKEN**: Your Telegram bot token (from @BotFather)
4. **TELEGRAM_CHAT_ID**: Your Telegram chat ID to receive notifications

### How to Get Credentials

**Binance API Keys:**
1. Log in to Binance
2. Go to Account → API Management
3. Create a new API key
4. Enable "Enable Reading" permission (no trading permissions needed)

**Telegram Bot:**
1. Message @BotFather on Telegram
2. Send `/newbot` and follow instructions
3. Copy the bot token provided
4. Start a chat with your bot
5. Get your chat ID by messaging @userinfobot

## How It Works

1. Fetches top N cryptocurrency pairs by 24h trading volume
2. For each pair, retrieves minute-by-minute price and volume data
3. Calculates price change over the lookback window
4. Compares recent volume to historical average
5. If price change exceeds threshold AND volume spike is detected:
   - Sends detailed Telegram notification with:
     - Symbol and direction (UP/DOWN)
     - Percentage change and time window
     - Recent volume vs average
     - Current price and market type
     - Direct link to Binance trading page
6. Implements cooldown to avoid repeated alerts for same symbol

## User Preferences
None specified yet.
