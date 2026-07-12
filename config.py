"""
Wave Trader Bot — Configuration
=================================
Credentials loaded from environment variables (Railway).
Never put real credentials in this file — keep it safe to commit.

Set these in Railway dashboard → Your Service → Variables:
  CLIENT_ID
  CLIENT_SECRET
  ACCOUNT_ID
  ACCESS_TOKEN  (filled in after first OAuth exchange)
  REDIRECT_URI
"""

import os

# ─── Spotware / cTrader API ──────────────────────────────────────────
CLIENT_ID     = os.environ.get("CLIENT_ID",     "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
REDIRECT_URI  = os.environ.get("REDIRECT_URI",  "")
ACCESS_TOKEN  = os.environ.get("ACCESS_TOKEN",  "")
ACCOUNT_ID    = int(os.environ.get("ACCOUNT_ID", "0"))

# cTrader endpoints
AUTH_URL  = "https://connect.spotware.com/oauth/token"
API_HOST  = "demo.ctraderapi.com"
API_PORT  = 5035

# ─── Strategy Parameters ─────────────────────────────────────────────
WINDOW_HOURS   = 2.5    # Hours from daily open to look for setups
RR_TARGET      = 3.0    # Reward:Risk ratio
TOLERANCE_PCT  = 0.35   # Retest level tolerance (35% of box range)
RISK_PER_TRADE = 0.01   # 1% of account per trade

# ─── Asset Configuration ─────────────────────────────────────────────
ASSETS = [
    {
        "name":      "Gold",
        "symbol":    "XAUUSD",
        "leverage":  20,
        "macd_mode": "exclude_counter",
        "session":   "commodity",
        "active":    True,
        "priority":  1,
    },
    {
        "name":      "Copper",
        "symbol":    "XCUUSD",
        "leverage":  20,
        "macd_mode": "trending",
        "session":   "commodity",
        "active":    True,
        "priority":  2,
    },
    {
        "name":      "Silver",
        "symbol":    "XAGUSD",
        "leverage":  20,
        "macd_mode": "exclude_counter",
        "session":   "commodity",
        "active":    True,
        "priority":  3,
    },
    {
        "name":      "Crude Oil",
        "symbol":    "XTIUSD",
        "leverage":  20,
        "macd_mode": "exclude_counter",
        "session":   "commodity",
        "active":    True,
        "priority":  4,
    },
    # Equities — enable Monday 14:30 UTC
    {
        "name":      "Microsoft",
        "symbol":    "MSFT",
        "leverage":  5,
        "macd_mode": "exclude_counter",
        "session":   "equity",
        "active":    False,
        "priority":  5,
    },
    {
        "name":      "S&P500",
        "symbol":    "SPX500",
        "leverage":  5,
        "macd_mode": "none",
        "session":   "equity",
        "active":    False,
        "priority":  6,
    },
    {
        "name":      "Apple",
        "symbol":    "AAPL",
        "leverage":  5,
        "macd_mode": "correct_side",
        "session":   "equity",
        "active":    False,
        "priority":  7,
    },
]

# MACD settings (computed on 1h bars)
MACD_FAST              = 12
MACD_SLOW              = 26
MACD_SIGNAL            = 9
MACD_COUNTER_THRESHOLD = 0.30

# Bot behaviour
MAX_CONCURRENT_TRADES = 7
CHECK_INTERVAL_SEC    = 60
LOG_LEVEL             = "INFO"
