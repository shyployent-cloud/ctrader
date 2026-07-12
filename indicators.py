"""
Wave Trader Bot — Indicator Library
=====================================
All technical indicator calculations used by the strategy.
Designed to work on both historical (backtest) and live (tick) data.
"""

import numpy as np
import pandas as pd


def compute_macd(closes, fast=12, slow=26, signal=9):
    """
    Computes MACD on a Series or list of close prices.
    Returns dict with current bar values only — the bot only needs
    the most recent reading, not the full history.
    """
    s = pd.Series(closes)
    ema_f  = s.ewm(span=fast, adjust=False).mean()
    ema_s  = s.ewm(span=slow, adjust=False).mean()
    macd   = ema_f - ema_s
    sig    = macd.ewm(span=signal, adjust=False).mean()
    hist   = macd - sig

    # Rolling average of absolute histogram for relative strength
    hist_range = hist.abs().rolling(20).mean()

    if len(hist) < 3:
        return None

    cur   = hist.iloc[-1]
    prev  = hist.iloc[-2]
    hr    = float(hist_range.iloc[-1]) if float(hist_range.iloc[-1]) > 0 else 1e-9
    rel   = abs(float(cur)) / hr

    return {
        "hist":          float(cur),
        "hist_prev":     float(prev),
        "hist_range":    hr,
        "trending_up":   float(cur) > float(prev),
        "trending_dn":   float(cur) < float(prev),
        "positive":      float(cur) > 0,
        "negative":      float(cur) < 0,
        "rel_strength":  rel,
    }


def macd_confirms(macd_data, direction, mode, threshold=0.30):
    """
    Returns True if MACD condition passes for the given mode.

    direction: "bull" or "bear"
    mode:
      "none"            — V5: no filter, always passes
      "exclude_counter" — V4a: only exclude strong counter-momentum
      "correct_side"    — V4b: histogram must be on correct side of zero
      "weak_trend"      — V4c: trending OR near zero
      "trending"        — V4_Loose: must be actively trending right way
    """
    if macd_data is None:
        return mode == "none"

    if mode == "none":
        return True

    if mode == "trending":
        return (macd_data["trending_up"] if direction == "bull"
                else macd_data["trending_dn"])

    if mode == "correct_side":
        return (macd_data["positive"] if direction == "bull"
                else macd_data["negative"])

    if mode == "weak_trend":
        near_zero = macd_data["rel_strength"] < 0.30
        trending  = (macd_data["trending_up"] if direction == "bull"
                     else macd_data["trending_dn"])
        return trending or near_zero

    if mode == "exclude_counter":
        strongly_counter = (
            (macd_data["trending_dn"] and direction == "bull"
             and macd_data["rel_strength"] > threshold) or
            (macd_data["trending_up"] and direction == "bear"
             and macd_data["rel_strength"] > threshold)
        )
        return not strongly_counter

    return True


def is_engulfing(curr, prev, direction="bull"):
    """
    Detects bullish or bearish engulfing candle pattern.

    curr/prev: dicts with keys open, high, low, close
    Returns True if the pattern is confirmed.
    """
    co = float(curr["open"]);  cc = float(curr["close"])
    po = float(prev["open"]);  pc = float(prev["close"])

    if direction == "bull":
        # Previous candle red, current green, current body engulfs previous
        return (pc < po and cc > co and cc > po and co < pc)
    else:
        # Previous candle green, current red, current body engulfs previous
        return (pc > po and cc < co and cc < po and co > pc)


def get_daily_box(prev_day_candle):
    """
    Returns the high/low box from yesterday's daily candle.
    prev_day_candle: dict with high, low keys
    """
    bhi = float(prev_day_candle["high"])
    blo = float(prev_day_candle["low"])
    if bhi <= blo:
        return None
    return {"high": bhi, "low": blo, "range": bhi - blo}


def check_breakout(candle_close, box):
    """
    Returns "bull" if close broke above box high,
            "bear" if close broke below box low,
            None if no breakout.
    """
    if candle_close > box["high"]:
        return "bull"
    if candle_close < box["low"]:
        return "bear"
    return None


def near_retest_level(candle, box, direction, tolerance_pct=0.35):
    """
    Returns True if the candle is within tolerance of the retest level.
    Retest level = box high for bull breakout, box low for bear breakout.
    Tolerance = tolerance_pct × box range.
    """
    level = box["high"] if direction == "bull" else box["low"]
    tol   = box["range"] * tolerance_pct
    price = float(candle["low"]) if direction == "bull" else float(candle["high"])
    return abs(price - level) <= tol


def compute_trade_levels(entry_price, direction, sl_distance, rr_target=3.0):
    """
    Computes TP and SL prices from entry.
    Returns dict with entry, sl, tp and sl_pct (stop loss as % of entry).
    """
    if direction == "bull":
        sl = entry_price - sl_distance
        tp = entry_price + sl_distance * rr_target
    else:
        sl = entry_price + sl_distance
        tp = entry_price - sl_distance * rr_target

    return {
        "entry":  round(entry_price, 6),
        "sl":     round(sl, 6),
        "tp":     round(tp, 6),
        "sl_pct": round(sl_distance / entry_price * 100, 4),
    }
