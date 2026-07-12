"""
Wave Trader Bot — cTrader Open API (Official SDK)
===================================================
Break & Bounce + 1h MACD Confluence strategy.
Uses Spotware's official Python SDK (Twisted/Protobuf TCP).

Authentication flow:
  1. App auth:    ProtoOAApplicationAuthReq
  2. Account list: ProtoOAGetAccountListByAccessTokenReq
  3. Account auth: ProtoOAAccountAuthReq
  4. Get symbols:  ProtoOASymbolsListReq
  5. Subscribe to spots: ProtoOASubscribeSpotsReq
  6. Get trend bars (candles): ProtoOAGetTrendbarsReq
  7. Place orders: ProtoOANewOrderReq

Strategy logic runs every CHECK_INTERVAL_SEC via a Twisted
LoopingCall that fetches candles and checks conditions.
"""

import os, sys, json, logging
from datetime import datetime, timezone, timedelta
from twisted.internet import reactor, defer, ssl, task
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

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

# ─── Global State ─────────────────────────────────────────────────────
client       = None
account_id   = None
symbol_map   = {}   # name -> ctid symbol id
asset_states = {}   # asset name -> state dict

# ─── Per-asset State ──────────────────────────────────────────────────
def make_state(asset_cfg):
    return {
        "cfg":         asset_cfg,
        "name":        asset_cfg["name"],
        "symbol":      asset_cfg["symbol"],
        "state":       "WAITING",
        "daily_box":   None,
        "breakout_dir": None,
        "today_str":   None,
        "in_trade":    False,
    }

# ─── Helpers ──────────────────────────────────────────────────────────
def session_window(asset_cfg):
    """Returns (open_utc, end_utc) for today's trading window."""
    now   = datetime.now(timezone.utc)
    today = now.date()
    if asset_cfg["session"] == "equity":
        open_t = datetime(today.year, today.month, today.day,
                          14, 30, tzinfo=timezone.utc)
    else:
        open_t = datetime(today.year, today.month, today.day,
                          0, 0, tzinfo=timezone.utc)
    return open_t, open_t + timedelta(hours=config.WINDOW_HOURS)

def within_window(asset_cfg):
    o, e = session_window(asset_cfg)
    return o <= datetime.now(timezone.utc) <= e

def calculate_volume(balance, sl_pct, leverage):
    risk   = balance * config.RISK_PER_TRADE
    volume = risk / (sl_pct / 100) / leverage
    volume = max(0.01, round(volume / 0.01) * 0.01)
    return min(volume, 1.0)

# ─── cTrader Message Helpers ──────────────────────────────────────────
def send(request):
    """Send a protobuf message and return a deferred."""
    return client.send(request)

def get_trendbars(symbol_id, period, count=100):
    """
    Fetch historical bars.
    period: ProtoOATrendbarPeriod (H1=4, M15=3, M5=2, D1=5)
    """
    req = ProtoOAGetTrendbarsReq()
    req.ctidTraderAccountId = account_id
    req.symbolId            = symbol_id
    req.period              = period
    req.count               = count
    return send(req)

def place_order(symbol_id, direction, volume_lots, sl, tp, label):
    """
    Places a market order with SL and TP.
    direction: ProtoOATradeSide.BUY or SELL
    volume_lots: in lots, converted to cTrader units (x100)
    sl/tp: absolute price values
    """
    req = ProtoOANewOrderReq()
    req.ctidTraderAccountId = account_id
    req.symbolId            = symbol_id
    req.orderType           = ProtoOAOrderType.MARKET
    req.tradeSide           = direction
    req.volume              = int(volume_lots * 100)
    req.stopLoss            = sl
    req.takeProfit          = tp
    req.label               = label
    return send(req)

# ─── Strategy Check ───────────────────────────────────────────────────
def parse_bars(res):
    """Convert ProtoOAGetTrendbarsRes to list of OHLC dicts."""
    bars = []
    for b in res.trendbar:
        close = b.low + b.deltaClose
        high  = b.low + b.deltaHigh
        o     = b.low + b.deltaOpen if hasattr(b, 'deltaOpen') else close
        bars.append({
            "open":  o  / 100000,
            "high":  high / 100000,
            "low":   b.low / 100000,
            "close": close / 100000,
        })
    return bars

@defer.inlineCallbacks
def check_asset(state):
    """
    Main strategy check for a single asset.
    Uses inlineCallbacks to write async code sequentially.
    """
    name    = state["name"]
    symbol  = state["symbol"]
    cfg     = state["cfg"]

    # Reset on new day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != state["today_str"]:
        state["today_str"]    = today
        state["state"]        = "WAITING"
        state["daily_box"]    = None
        state["breakout_dir"] = None
        state["in_trade"]     = False
        log.info(f"[{name}] New day — reset")

    if state["in_trade"]:
        return  # cTrader manages SL/TP

    if not within_window(cfg):
        return  # Outside window

    sym_id = symbol_map.get(symbol)
    if not sym_id:
        log.warning(f"[{name}] Symbol {symbol} not found in symbol map")
        return

    try:
        # Fetch daily candles for yesterday's box
        d1_res  = yield get_trendbars(sym_id, ProtoOATrendbarPeriod.D1,  3)
        h1_res  = yield get_trendbars(sym_id, ProtoOATrendbarPeriod.H1,  50)
        m15_res = yield get_trendbars(sym_id, ProtoOATrendbarPeriod.M15, 20)
        m5_res  = yield get_trendbars(sym_id, ProtoOATrendbarPeriod.M5,  10)

        d1_bars  = parse_bars(d1_res)
        h1_bars  = parse_bars(h1_res)
        m15_bars = parse_bars(m15_res)
        m5_bars  = parse_bars(m5_res)

        if len(d1_bars) < 2 or len(h1_bars) < 30:
            return

        # Step 1 — Daily box
        if state["daily_box"] is None:
            box = get_daily_box(d1_bars[-2])
            if box:
                state["daily_box"] = box
                log.info(f"[{name}] Box: H={box['high']:.5f} L={box['low']:.5f}")

        if state["daily_box"] is None:
            return

        box = state["daily_box"]

        # Step 2 — 15m breakout
        if state["state"] == "WAITING":
            if len(m15_bars) < 1:
                return
            bdir = check_breakout(m15_bars[-1]["close"], box)
            if bdir:
                state["breakout_dir"] = bdir
                state["state"]        = "BREAKOUT"
                log.info(f"[{name}] BREAKOUT → {bdir.upper()}")

        # Step 3 — 5m engulfing at retest
        if state["state"] == "BREAKOUT":
            if len(m5_bars) < 2:
                return

            curr = m5_bars[-1]
            prev = m5_bars[-2]
            bdir = state["breakout_dir"]

            if not near_retest_level(curr, box, bdir, config.TOLERANCE_PCT):
                return

            if not is_engulfing(curr, prev, bdir):
                return

            log.info(f"[{name}] Engulfing candle at retest ✓")

            # MACD confluence on 1h
            closes_1h = [b["close"] for b in h1_bars]
            macd_data = compute_macd(closes_1h,
                                     config.MACD_FAST,
                                     config.MACD_SLOW,
                                     config.MACD_SIGNAL)

            if not macd_confirms(macd_data, bdir,
                                 cfg["macd_mode"],
                                 config.MACD_COUNTER_THRESHOLD):
                log.info(f"[{name}] MACD filter rejected")
                return

            log.info(f"[{name}] MACD confirmed ✓")

            # Trade levels
            if bdir == "bull":
                entry = float(prev["high"])
                sl_d  = entry - float(curr["low"]) * 0.999
            else:
                entry = float(prev["low"])
                sl_d  = float(curr["high"]) * 1.001 - entry

            if sl_d <= 0:
                return

            levels = compute_trade_levels(entry, bdir, sl_d, config.RR_TARGET)

            # Get account balance
            bal_req = ProtoOATraderReq()
            bal_req.ctidTraderAccountId = account_id
            bal_res = yield send(bal_req)
            balance = bal_res.trader.balance / 100  # cents to currency

            volume = calculate_volume(balance, levels["sl_pct"], cfg["leverage"])
            direction = ProtoOATradeSide.BUY if bdir == "bull" else ProtoOATradeSide.SELL
            label = f"WaveTrader_{symbol}"

            log.info(f"[{name}] PLACING ORDER → "
                     f"{'BUY' if bdir=='bull' else 'SELL'} "
                     f"entry={levels['entry']} "
                     f"SL={levels['sl']} TP={levels['tp']} "
                     f"vol={volume}")

            yield place_order(sym_id, direction, volume,
                              levels["sl"], levels["tp"], label)

            state["state"]    = "IN_TRADE"
            state["in_trade"] = True
            log.info(f"[{name}] ORDER PLACED ✓")

            # Log to file
            with open("trades.log", "a") as f:
                f.write(json.dumps({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "asset":     name,
                    "direction": "BUY" if bdir == "bull" else "SELL",
                    "entry":     levels["entry"],
                    "sl":        levels["sl"],
                    "tp":        levels["tp"],
                    "volume":    volume,
                }) + "\n")

    except Exception as e:
        log.error(f"[{name}] Error in check: {e}")

@defer.inlineCallbacks
def run_strategy_cycle():
    """Called every CHECK_INTERVAL_SEC — checks all active assets."""
    log.debug(f"Strategy cycle — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    for state in asset_states.values():
        yield check_asset(state)

# ─── Connection & Auth Flow ───────────────────────────────────────────
def on_message_received(client, message):
    """Global message handler — logs errors."""
    if message.payloadType == ProtoOAErrorRes.payloadType.DESCRIPTOR.full_name:
        err = Protobuf.extract(message)
        log.error(f"API Error: {err.errorCode} — {err.description}")

def on_error(failure):
    log.error(f"Connection error: {failure}")

@defer.inlineCallbacks
def on_connected(client_instance):
    global client, account_id, symbol_map

    log.info("TCP connection established ✓")

    # Step 1 — App auth
    app_req = ProtoOAApplicationAuthReq()
    app_req.clientId     = config.CLIENT_ID
    app_req.clientSecret = config.CLIENT_SECRET
    app_res = yield client_instance.send(app_req)
    log.info("App authenticated ✓")

    # Step 2 — Get account list
    acc_req = ProtoOAGetAccountListByAccessTokenReq()
    acc_req.accessToken = config.ACCESS_TOKEN
    acc_res = yield client_instance.send(acc_req)

    # Find our account
    accounts = acc_res.ctidTraderAccount
    if not accounts:
        log.error("No accounts found for this access token")
        reactor.stop()
        return

    # Use configured ACCOUNT_ID or first available
    target_id = config.ACCOUNT_ID
    matched   = [a for a in accounts if a.ctidTraderAccountId == target_id]
    if matched:
        account_id = matched[0].ctidTraderAccountId
    else:
        account_id = accounts[0].ctidTraderAccountId
        log.warning(f"ACCOUNT_ID {target_id} not found — "
                    f"using {account_id}")

    log.info(f"Using account ID: {account_id}")

    # Step 3 — Account auth
    auth_req = ProtoOAAccountAuthReq()
    auth_req.ctidTraderAccountId = account_id
    auth_req.accessToken         = config.ACCESS_TOKEN
    yield client_instance.send(auth_req)
    log.info("Account authenticated ✓")

    # Step 4 — Get symbol list and build name→id map
    sym_req = ProtoOASymbolsListReq()
    sym_req.ctidTraderAccountId = account_id
    sym_res = yield client_instance.send(sym_req)

    for s in sym_res.symbol:
        symbol_map[s.symbolName] = s.symbolId

    log.info(f"Loaded {len(symbol_map)} symbols")

    # Verify all active assets have valid symbols
    active = [a for a in config.ASSETS if a["active"]]
    for a in active:
        if a["symbol"] not in symbol_map:
            log.warning(f"Symbol {a['symbol']} not found — "
                        f"check exact name in cTrader")
        else:
            log.info(f"  ✓ {a['name']} → {a['symbol']} "
                     f"(id={symbol_map[a['symbol']]})")

    # Initialise asset states
    for a in active:
        asset_states[a["name"]] = make_state(a)

    log.info("=" * 55)
    log.info(f"  Wave Trader Bot — RUNNING")
    log.info(f"  Monitoring: {', '.join(asset_states.keys())}")
    log.info(f"  Check interval: {config.CHECK_INTERVAL_SEC}s")
    log.info(f"  RR: {config.RR_TARGET}:1 | "
             f"Risk: {config.RISK_PER_TRADE*100:.0f}%/trade")
    log.info("=" * 55)

    # Start strategy loop
    loop = task.LoopingCall(run_strategy_cycle)
    loop.start(config.CHECK_INTERVAL_SEC)

# ─── Entry Point ──────────────────────────────────────────────────────
def main():
    global client

    log.info("Wave Trader Bot — Starting Up")
    log.info("Strategy: Break & Bounce + 1h MACD Confluence")

    # Validate config
    if not config.CLIENT_ID:
        log.error("CLIENT_ID not set — check Railway environment variables")
        sys.exit(1)
    if not config.ACCESS_TOKEN:
        log.error("ACCESS_TOKEN not set — complete OAuth flow first")
        sys.exit(1)

    # Connect to cTrader demo via TCP/TLS
    host = EndPoints.PROTOBUF_DEMO_HOST
    port = EndPoints.PROTOBUF_PORT

    log.info(f"Connecting to {host}:{port}...")

    client = Client(host, port, TcpProtocol)
    client.setConnectedCallback(on_connected)
    client.setMessageReceivedCallback(on_message_received)
    client.setDisconnectedCallback(
        lambda c: log.warning("Disconnected — reconnecting..."))

    # Start Twisted reactor
    reactor.run()

if __name__ == "__main__":
    main()
