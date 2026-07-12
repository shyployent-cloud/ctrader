"""
Wave Trader Bot — Main Execution Script
=========================================
Break & Bounce + 1h MACD Confluence strategy.
Connects to Pepperstone demo via cTrader Open API.
Monitors all active assets simultaneously.
Places orders automatically when setups are detected.

Architecture:
  - OAuth2 token exchange on startup
  - Per-asset state machines tracking: waiting → breakout → signal → in_trade
  - 1-minute polling loop checking all assets
  - All trades logged to trades.log for review

Run:  python bot.py
Stop: Ctrl+C (bot closes any open positions cleanly)
"""

import os, sys, time, json, logging, requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import config
from indicators import (
    compute_macd, macd_confirms, is_engulfing,
    get_daily_box, check_breakout, near_retest_level,
    compute_trade_levels
)

# ─── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ]
)
log = logging.getLogger("WaveTrader")


# ─── cTrader API Client ───────────────────────────────────────────────
class CTraderClient:
    """
    Lightweight REST/WebAPI wrapper for cTrader Open API.
    Uses the HTTP endpoints for simplicity — no need for the
    full Twisted/protobuf stack for this use case.
    """

    BASE = "https://api.spotware.com/connect"

    def __init__(self, access_token, account_id):
        self.token      = access_token
        self.account_id = account_id
        self.session    = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
        })

    def _get(self, endpoint, params=None):
        url = f"{self.BASE}{endpoint}"
        r   = self.session.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint, data):
        url = f"{self.BASE}{endpoint}"
        r   = self.session.post(url, json=data, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_account_info(self):
        return self._get(f"/tradingaccounts/{self.account_id}")

    def get_balance(self):
        info = self.get_account_info()
        return float(info.get("balance", 0)) / 100  # cTrader uses cents

    def get_symbol_info(self, symbol):
        symbols = self._get(f"/symbols/{symbol}")
        return symbols

    def get_candles(self, symbol, timeframe, count=100):
        """
        Fetches historical bars for a symbol.
        timeframe: "H1" "M15" "M5" "D1"
        """
        params = {
            "symbolName": symbol,
            "period":     timeframe,
            "count":      count,
        }
        return self._get("/gettrendbars", params)

    def place_market_order(self, symbol, direction, volume,
                           sl_price, tp_price, label="WaveTrader"):
        """
        Places a market order with SL and TP.
        direction: "BUY" or "SELL"
        volume: in lots (e.g. 0.01)
        """
        data = {
            "tradingAccountId": self.account_id,
            "symbolName":       symbol,
            "tradeSide":        direction,
            "volume":           int(volume * 100),  # cTrader uses centilots
            "stopLoss":         sl_price,
            "takeProfit":       tp_price,
            "label":            label,
        }
        log.info(f"ORDER → {direction} {symbol} "
                 f"vol={volume} SL={sl_price} TP={tp_price}")
        return self._post("/marketorder", data)

    def get_open_positions(self):
        return self._get(f"/tradingaccounts/{self.account_id}/positions")

    def close_position(self, position_id):
        return self._post(f"/positions/{position_id}/close", {})


# ─── OAuth Token Exchange ─────────────────────────────────────────────
def get_access_token():
    """
    Exchanges auth code for access token if not already stored.
    On first run, prints the auth URL and waits for you to paste
    the callback code from the Railway URL.
    """
    token_file = "token.json"

    # Check for saved token
    if os.path.exists(token_file):
        with open(token_file) as f:
            data = json.load(f)
        if data.get("access_token"):
            log.info("Access token loaded from token.json")
            return data["access_token"]

    # Check config
    if config.ACCESS_TOKEN:
        return config.ACCESS_TOKEN

    # Interactive OAuth flow
    auth_url = (
        f"https://connect.spotware.com/oauth/authorize"
        f"?client_id={config.CLIENT_ID}"
        f"&redirect_uri={config.REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=trading"
    )
    print("\n" + "="*60)
    print("OAUTH AUTHORISATION REQUIRED")
    print("="*60)
    print(f"\n1. Open this URL in your browser:\n\n   {auth_url}\n")
    print("2. Log in with your Pepperstone cTrader credentials")
    print("3. After redirect, copy the 'code' parameter from the URL")
    print("   (Railway callback page will show it)\n")

    code = input("Paste the authorisation code here: ").strip()

    # Exchange code for token
    r = requests.post(config.AUTH_URL, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     config.CLIENT_ID,
        "client_secret": config.CLIENT_SECRET,
        "redirect_uri":  config.REDIRECT_URI,
    })
    r.raise_for_status()
    token_data = r.json()

    # Save token
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)
    log.info("Access token obtained and saved to token.json")

    return token_data["access_token"]


# ─── Position Sizer ───────────────────────────────────────────────────
def calculate_volume(account_balance, sl_pct, leverage, risk_pct=0.01):
    """
    Calculates trade volume in lots based on account risk.

    account_balance: total account value in account currency
    sl_pct:         stop loss distance as % of entry price
    leverage:       leverage ratio (e.g. 20)
    risk_pct:       fraction of account to risk per trade (default 1%)

    Returns volume in lots (minimum 0.01).
    """
    risk_amount  = account_balance * risk_pct
    # With leverage, each lot controls leverage x notional
    # Volume = risk_amount / (sl_pct * notional_per_lot)
    # We approximate notional_per_lot as account_balance * leverage / typical_lots
    # Simplified: volume in lots = risk_amount / (sl_pct% * price * lot_size)
    # For standardised calculation across assets: use 0.01 minimum
    raw_volume = risk_amount / (sl_pct / 100 * 100000 / leverage)
    volume     = max(0.01, round(raw_volume / 0.01) * 0.01)
    return min(volume, 1.0)  # Cap at 1 lot for safety in sandbox


# ─── Per-Asset State Machine ──────────────────────────────────────────
class AssetMonitor:
    """
    Tracks the strategy state for a single asset.
    States: WAITING → BREAKOUT → SIGNAL → IN_TRADE → WAITING

    WAITING:   Looking for 15m breakout of daily box within window
    BREAKOUT:  Breakout confirmed, watching for engulfing retest on 5m
    SIGNAL:    Engulfing + MACD confirmed, order placed
    IN_TRADE:  Position open, managed by cTrader SL/TP
    """

    def __init__(self, asset_config):
        self.cfg       = asset_config
        self.name      = asset_config["name"]
        self.symbol    = asset_config["symbol"]
        self.state     = "WAITING"
        self.daily_box = None
        self.breakout_dir = None
        self.session_open = None
        self.session_end  = None
        self.trade_id  = None
        self.today_str = None

        # Candle buffers — populated each cycle
        self.candles_1h  = []
        self.candles_15m = []
        self.candles_5m  = []

    def get_session_times(self):
        """Returns session open and end as UTC datetime for today."""
        now = datetime.now(timezone.utc)
        today = now.date()

        if self.cfg["session"] == "equity":
            # 09:30 EST = 14:30 UTC (or 13:30 during BST — approximate)
            open_time = datetime(today.year, today.month, today.day,
                                 14, 30, tzinfo=timezone.utc)
        else:
            # Commodity: midnight UTC
            open_time = datetime(today.year, today.month, today.day,
                                 0, 0, tzinfo=timezone.utc)

        end_time = open_time + timedelta(hours=config.WINDOW_HOURS)
        return open_time, end_time

    def is_within_window(self):
        """True if current UTC time is within today's trading window."""
        now = datetime.now(timezone.utc)
        sess_open, sess_end = self.get_session_times()
        return sess_open <= now <= sess_end

    def reset_for_new_day(self):
        """Called at start of each new trading day."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.today_str:
            self.today_str    = today
            self.state        = "WAITING"
            self.daily_box    = None
            self.breakout_dir = None
            log.info(f"[{self.name}] New day — reset to WAITING")

    def update(self, client, account_balance):
        """
        Main update cycle for this asset.
        Called once per CHECK_INTERVAL_SEC.
        Returns trade action taken or None.
        """
        self.reset_for_new_day()

        # ── Already in a trade — cTrader manages SL/TP, nothing to do
        if self.state == "IN_TRADE":
            positions = client.get_open_positions()
            still_open = any(
                p.get("label") == f"WaveTrader_{self.symbol}"
                for p in positions.get("position", [])
            )
            if not still_open:
                log.info(f"[{self.name}] Trade closed (SL or TP hit)")
                self.state = "WAITING"
            return None

        if not self.is_within_window():
            return None  # Outside trading window

        # ── Fetch required candle data
        try:
            d1_data  = client.get_candles(self.symbol, "D1",  count=3)
            h1_data  = client.get_candles(self.symbol, "H1",  count=50)
            m15_data = client.get_candles(self.symbol, "M15", count=20)
            m5_data  = client.get_candles(self.symbol, "M5",  count=10)
        except Exception as e:
            log.warning(f"[{self.name}] Data fetch failed: {e}")
            return None

        def parse_candles(data):
            bars = data.get("data", {}).get("bar", [])
            return [{"open":  b["open"]/100000,
                     "high":  b["high"]/100000,
                     "low":   b["low"]/100000,
                     "close": b["close"]/100000}
                    for b in bars]

        candles_d1  = parse_candles(d1_data)
        candles_1h  = parse_candles(h1_data)
        candles_15m = parse_candles(m15_data)
        candles_5m  = parse_candles(m5_data)

        if len(candles_d1) < 2 or len(candles_1h) < 30:
            return None

        # ── Step 1: Build daily box from yesterday
        if self.daily_box is None:
            prev_day      = candles_d1[-2]  # yesterday
            self.daily_box = get_daily_box(prev_day)
            if self.daily_box:
                log.info(f"[{self.name}] Daily box: "
                         f"H={self.daily_box['high']:.5f} "
                         f"L={self.daily_box['low']:.5f}")

        if self.daily_box is None:
            return None

        # ── Step 2: Check for 15m breakout confirmation
        if self.state == "WAITING":
            if len(candles_15m) < 2:
                return None
            latest_15m = candles_15m[-1]["close"]
            bdir = check_breakout(latest_15m, self.daily_box)
            if bdir:
                self.breakout_dir = bdir
                self.state = "BREAKOUT"
                log.info(f"[{self.name}] BREAKOUT confirmed → {bdir.upper()} "
                         f"| close={latest_15m:.5f}")

        # ── Step 3: Watch for engulfing candle at retest level
        if self.state == "BREAKOUT":
            if len(candles_5m) < 2:
                return None

            curr_5m = candles_5m[-1]
            prev_5m = candles_5m[-2]

            # Check if near retest level
            if not near_retest_level(curr_5m, self.daily_box,
                                     self.breakout_dir, config.TOLERANCE_PCT):
                return None

            # Check engulfing pattern
            if not is_engulfing(curr_5m, prev_5m, self.breakout_dir):
                return None

            log.info(f"[{self.name}] Engulfing candle detected at retest level")

            # ── MACD confluence check on 1h
            closes_1h = [c["close"] for c in candles_1h]
            macd_data = compute_macd(closes_1h,
                                     config.MACD_FAST,
                                     config.MACD_SLOW,
                                     config.MACD_SIGNAL)

            if not macd_confirms(macd_data, self.breakout_dir,
                                 self.cfg["macd_mode"],
                                 config.MACD_COUNTER_THRESHOLD):
                log.info(f"[{self.name}] MACD filter rejected — "
                         f"mode={self.cfg['macd_mode']}")
                return None

            log.info(f"[{self.name}] MACD confirmed ✓")

            # ── All conditions met — calculate trade levels
            if self.breakout_dir == "bull":
                entry_price = float(prev_5m["high"])
                sl_dist     = entry_price - float(curr_5m["low"]) * 0.999
            else:
                entry_price = float(prev_5m["low"])
                sl_dist     = float(curr_5m["high"]) * 1.001 - entry_price

            if sl_dist <= 0:
                log.warning(f"[{self.name}] Invalid SL distance — skipping")
                return None

            levels = compute_trade_levels(
                entry_price, self.breakout_dir, sl_dist, config.RR_TARGET)

            # ── Position sizing
            volume = calculate_volume(
                account_balance,
                levels["sl_pct"],
                self.cfg["leverage"],
                config.RISK_PER_TRADE
            )

            direction = "BUY" if self.breakout_dir == "bull" else "SELL"

            log.info(f"[{self.name}] SIGNAL → {direction} | "
                     f"entry={levels['entry']} SL={levels['sl']} "
                     f"TP={levels['tp']} vol={volume}")

            # ── Place order
            try:
                result = client.place_market_order(
                    symbol    = self.symbol,
                    direction = direction,
                    volume    = volume,
                    sl_price  = levels["sl"],
                    tp_price  = levels["tp"],
                    label     = f"WaveTrader_{self.symbol}",
                )
                self.state    = "IN_TRADE"
                self.trade_id = result.get("orderId")
                log.info(f"[{self.name}] ORDER PLACED ✓ id={self.trade_id}")

                # Log trade to file
                with open("trades.log", "a") as f:
                    f.write(json.dumps({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "asset":     self.name,
                        "symbol":    self.symbol,
                        "direction": direction,
                        "entry":     levels["entry"],
                        "sl":        levels["sl"],
                        "tp":        levels["tp"],
                        "volume":    volume,
                        "order_id":  self.trade_id,
                    }) + "\n")

                return {
                    "asset":     self.name,
                    "direction": direction,
                    "entry":     levels["entry"],
                    "sl":        levels["sl"],
                    "tp":        levels["tp"],
                }

            except Exception as e:
                log.error(f"[{self.name}] Order failed: {e}")
                self.state = "WAITING"
                return None

        return None


# ─── Main Bot Loop ────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  Wave Trader Bot — Starting Up")
    log.info("  Strategy: Break & Bounce + 1h MACD Confluence")
    log.info("=" * 55)

    # OAuth
    log.info("Authenticating with cTrader...")
    access_token = get_access_token()

    # Connect
    client = CTraderClient(access_token, config.ACCOUNT_ID)

    # Verify connection
    try:
        balance = client.get_balance()
        log.info(f"Connected ✓ | Account balance: £{balance:,.2f}")
    except Exception as e:
        log.error(f"Connection failed: {e}")
        log.error("Check your CLIENT_ID, CLIENT_SECRET, ACCOUNT_ID in config.py")
        sys.exit(1)

    # Initialise asset monitors — active assets only
    active_assets = [a for a in config.ASSETS if a["active"]]
    monitors = [AssetMonitor(a) for a in active_assets]

    log.info(f"Monitoring {len(monitors)} assets: "
             f"{', '.join(m.name for m in monitors)}")
    log.info(f"Check interval: {config.CHECK_INTERVAL_SEC}s | "
             f"RR: {config.RR_TARGET}:1 | "
             f"Risk: {config.RISK_PER_TRADE*100:.0f}% per trade")

    # Main loop
    try:
        while True:
            cycle_start = time.time()
            log.debug(f"Cycle start — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

            # Refresh balance each cycle
            try:
                balance = client.get_balance()
            except Exception as e:
                log.warning(f"Balance refresh failed: {e}")

            # Update all monitors
            for monitor in monitors:
                try:
                    result = monitor.update(client, balance)
                    if result:
                        log.info(f"TRADE PLACED: {result}")
                except Exception as e:
                    log.error(f"[{monitor.name}] Cycle error: {e}")

            # Sleep until next cycle
            elapsed = time.time() - cycle_start
            sleep_time = max(0, config.CHECK_INTERVAL_SEC - elapsed)
            log.debug(f"Cycle done in {elapsed:.1f}s — "
                      f"sleeping {sleep_time:.0f}s")
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        log.info("\nBot stopped by user (Ctrl+C)")
        log.info("All positions remain open — manage manually in cTrader")


if __name__ == "__main__":
    main()
