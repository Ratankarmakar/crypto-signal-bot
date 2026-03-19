import subprocess, sys

REQUIRED = [
    ("pandas",  "pandas"),
    ("numpy",   "numpy"),
    ("aiohttp", "aiohttp"),
    ("ccxt",    "ccxt"),
]

print("🔧 Checking packages...")
for import_name, pkg_name in REQUIRED:
    try:
        __import__(import_name)
    except ImportError:
        print(f"  Installing {pkg_name}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", pkg_name, "--quiet"],
            stderr=subprocess.DEVNULL,
        )

# Force python-telegram-bot v21+ (Python 3.13/3.14 compatible, no imghdr)
try:
    import telegram
    ver = tuple(int(x) for x in telegram.__version__.split(".")[:2])
    if ver < (21, 0):
        raise ImportError("old version")
except Exception:
    print("  Installing python-telegram-bot>=21.0 ...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install",
         "python-telegram-bot>=21.0", "--upgrade", "--quiet"],
        stderr=subprocess.DEVNULL,
    )

print("✅ All packages ready!")

# ══════════════════════════════════════════════════════════════════════
#  ⚙️  CONFIG — Edit only these 2 lines
# ══════════════════════════════════════════════════════════════════════

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"   # Get from @BotFather
CHAT_ID   = 0  # Replace with your chat ID (integer) from @userinfobot

DEFAULT_WATCHLIST = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT",
    "BNB/USDT", "XRP/USDT", "DOGE/USDT",
]
MAX_WATCHLIST = 25

DEFAULT_MODE = "balanced"
MODES = {
    "conservative": {"min_score": 8,  "atr_sl": 2.0, "atr_tp1": 1.5, "atr_tp2": 4.0},
    "balanced":     {"min_score": 6,  "atr_sl": 1.5, "atr_tp1": 1.2, "atr_tp2": 3.5},
    "sensitive":    {"min_score": 4,  "atr_sl": 1.2, "atr_tp1": 1.0, "atr_tp2": 2.5},
}

PRIMARY_TF   = "1h"
CONFIRM_TF   = "4h"
CANDLE_LIMIT = 300

RSI_PERIOD=14;  RSI_BUY=30;   RSI_SELL=70
EMA_FAST=20;    EMA_SLOW=50;  SMA_TREND=200
MACD_FAST=12;   MACD_SLOW=26; MACD_SIGNAL=9
BB_PERIOD=20;   BB_STD=2.0
ADX_PERIOD=14;  ADX_TRENDING=20
ATR_PERIOD=14;  VWAP_PERIOD=20
VOLUME_SPIKE_MULT = 1.5

SCAN_TIMES       = ["07:00","11:00","15:00","19:00","23:00"]
ALERT_CHECK_MINS = 5
USE_FEAR_GREED   = True
FG_MAX_FOR_LONG  = 79
FG_MIN_FOR_LONG  = 20

STATE_FILE = "signal_bot_state.json"

# ══════════════════════════════════════════════════════════════════════
import asyncio, json, logging, re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import ccxt.async_support as ccxt_async
import numpy as np
import pandas as pd
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("signal_bot")

_COIN_RE = re.compile(r"^[A-Z]{2,10}$")
def is_valid_coin(c): return bool(_COIN_RE.match(c))
def symbol_to_coin(s):        return s.replace("/USDT","")
def coin_to_symbol(c):
    c = c.upper().replace("USDT","").strip("/")
    return f"{c}/USDT"

# ══════════════════════════════════════════════════════════════════════
#  💾  STATE
# ══════════════════════════════════════════════════════════════════════

@dataclass
class BotState:
    watchlist:    list = field(default_factory=list)
    price_alerts: dict = field(default_factory=lambda: defaultdict(list))
    active_tf:    str  = PRIMARY_TF
    mode:         str  = DEFAULT_MODE
    rsi_buy:      int  = RSI_BUY
    rsi_sell:     int  = RSI_SELL

    def save(self):
        try:
            Path(STATE_FILE).write_text(json.dumps({
                "watchlist":    self.watchlist,
                "price_alerts": {k:v for k,v in self.price_alerts.items() if v},
                "active_tf":    self.active_tf,
                "mode":         self.mode,
                "rsi_buy":      self.rsi_buy,
                "rsi_sell":     self.rsi_sell,
            }, indent=2))
        except Exception as e: log.error(f"Save error: {e}")

    def load(self):
        try:
            if not Path(STATE_FILE).exists():
                self.watchlist = list(DEFAULT_WATCHLIST)
                return
            d = json.loads(Path(STATE_FILE).read_text())
            self.watchlist    = d.get("watchlist",    list(DEFAULT_WATCHLIST))
            self.active_tf    = d.get("active_tf",    PRIMARY_TF)
            self.mode         = d.get("mode",         DEFAULT_MODE)
            self.rsi_buy      = d.get("rsi_buy",      RSI_BUY)
            self.rsi_sell     = d.get("rsi_sell",     RSI_SELL)
            self.price_alerts = defaultdict(list, d.get("price_alerts", {}))
            log.info(f"State loaded — {len(self.watchlist)} coins")
        except Exception as e:
            log.error(f"Load error: {e}")
            self.watchlist = list(DEFAULT_WATCHLIST)


state    = BotState()
exchange = ccxt_async.binance({"enableRateLimit": True})

# ══════════════════════════════════════════════════════════════════════
#  📡  DATA FETCH
# ══════════════════════════════════════════════════════════════════════

async def fetch_ohlcv(symbol: str, tf: str = None) -> Optional[pd.DataFrame]:
    tf = tf or state.active_tf
    try:
        raw = await exchange.fetch_ohlcv(symbol, timeframe=tf, limit=CANDLE_LIMIT)
        if not raw or len(raw) < 60: 
            return None
        df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        log.error(f"OHLCV error [{symbol}]: {e}")
        return None

async def fetch_price(symbol: str) -> Optional[float]:
    try:
        ticker = await exchange.fetch_ticker(symbol)
        return float(ticker["last"])
    except Exception as e:
        log.error(f"Price error [{symbol}]: {e}")
        return None

async def fetch_fear_greed() -> Optional[int]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                data = await response.json()
                return int(data["data"][0]["value"])
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════
#  📊  INDICATORS — pure pandas/numpy (no pandas_ta)
# ══════════════════════════════════════════════════════════════════════

def calculate_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calculate_ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

def calculate_sma(close, period):
    return close.rolling(period).mean()

def calculate_macd(close, fast=12, slow=26, signal=9):
    macd = calculate_ema(close, fast) - calculate_ema(close, slow)
    signal_line = calculate_ema(macd, signal)
    return macd, signal_line, macd - signal_line

def calculate_bollinger_bands(close, period=20, std=2.0):
    middle = close.rolling(period).mean()
    std_dev = close.rolling(period).std()
    upper = middle + std * std_dev
    lower = middle - std * std_dev
    return upper, middle, lower

def calculate_atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(com=period-1, min_periods=period).mean()

def calculate_adx(high, low, close, period=14):
    up_move = high.diff()
    down_move = -low.diff()
    
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=close.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=close.index)
    
    atr = calculate_atr(high, low, close, period)
    
    plus_di = 100 * calculate_ema(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100 * calculate_ema(minus_dm, period) / atr.replace(0, np.nan)
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = calculate_ema(dx, period)
    
    return adx, plus_di, minus_di

def calculate_stoch_rsi(close, rsi_period=14, stoch_period=14, k_smoothing=3, d_smoothing=3):
    rsi = calculate_rsi(close, rsi_period)
    lowest_rsi = rsi.rolling(stoch_period).min()
    highest_rsi = rsi.rolling(stoch_period).max()
    
    stoch = 100 * (rsi - lowest_rsi) / (highest_rsi - lowest_rsi).replace(0, np.nan)
    k_line = stoch.rolling(k_smoothing).mean()
    d_line = k_line.rolling(d_smoothing).mean()
    
    return k_line, d_line

def calculate_obv(close, volume):
    return (np.sign(close.diff()).fillna(0) * volume).cumsum()

def calculate_vwap(high, low, close, volume, period=20):
    typical_price = (high + low + close) / 3
    return (typical_price * volume).rolling(period).sum() / volume.rolling(period).sum()

def detect_hammer(open_price, high, low, close):
    body = (close - open_price).abs()
    lower_wick = open_price.combine(close, min) - low
    upper_wick = high - open_price.combine(close, max)
    
    return ((lower_wick >= 2 * body) & 
            (upper_wick <= body * 0.5) & 
            (body > 0)).astype(int)

def detect_engulfing(open_price, close):
    prev_open = open_price.shift(1)
    prev_close = close.shift(1)
    
    bullish_engulfing = (prev_close < prev_open) & (close > prev_open) & (open_price < prev_close)
    bearish_engulfing = (prev_close > prev_open) & (close < prev_open) & (open_price > prev_close)
    
    result = pd.Series(0, index=close.index)
    result[bullish_engulfing] = 1
    result[bearish_engulfing] = -1
    return result

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_price = df["open"]
    volume = df["volume"]
    
    df["rsi"] = calculate_rsi(close, RSI_PERIOD)
    df["ema_fast"] = calculate_ema(close, EMA_FAST)
    df["ema_slow"] = calculate_ema(close, EMA_SLOW)
    df["sma200"] = calculate_sma(close, SMA_TREND)
    df["atr"] = calculate_atr(high, low, close, ATR_PERIOD)
    df["obv"] = calculate_obv(close, volume)
    df["macd"], df["macd_signal"], df["macd_histogram"] = calculate_macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    df["bb_upper"], df["bb_middle"], df["bb_lower"] = calculate_bollinger_bands(close, BB_PERIOD, BB_STD)
    df["adx"], df["plus_di"], df["minus_di"] = calculate_adx(high, low, close, ADX_PERIOD)
    df["stoch_k"], df["stoch_d"] = calculate_stoch_rsi(close, RSI_PERIOD, 14, 3, 3)
    df["vwap"] = calculate_vwap(high, low, close, volume, VWAP_PERIOD)
    df["volume_avg"] = volume.rolling(20).mean()
    df["volume_ratio"] = volume / df["volume_avg"].clip(lower=0.001)
    df["hammer"] = detect_hammer(open_price, high, low, close)
    df["engulfing"] = detect_engulfing(open_price, close)
    
    df.dropna(inplace=True)
    return df

def safe_get(row, col, default=0.0):
    try:
        val = float(row[col]) if col in row.index else default
        return val if pd.notna(val) else default
    except:
        return default

def compute_mtf_bias(df_4h):
    if df_4h is None or len(df_4h) < 3:
        return {"bias": "UNKNOWN", "score": 0}
    
    df_4h = compute_indicators(df_4h)
    if len(df_4h) < 3:
        return {"bias": "UNKNOWN", "score": 0}
    
    last = df_4h.iloc[-2]
    score = 0
    
    if safe_get(last, "ema_fast") > safe_get(last, "ema_slow"):
        score += 2
    else:
        score -= 2
    
    rsi_value = safe_get(last, "rsi")
    if rsi_value > 55:
        score += 1
    elif rsi_value < 45:
        score -= 1
    
    if safe_get(last, "macd") > safe_get(last, "macd_signal"):
        score += 1
    else:
        score -= 1
    
    if safe_get(last, "close") > safe_get(last, "sma200"):
        score += 1
    else:
        score -= 1
    
    if score >= 3:
        bias = "BULLISH"
    elif score <= -3:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    
    return {"bias": bias, "score": score}

def generate_signals(df, mtf=None, fear_greed=None):
    if len(df) < 4:
        return {}
    
    last = df.iloc[-2]
    prev = df.iloc[-3]
    price = float(last["close"])
    config = MODES[state.mode]
    
    score_components = {}
    patterns = []
    
    # RSI
    rsi_value = safe_get(last, "rsi")
    if rsi_value <= state.rsi_buy:
        rsi_signal = "OVERSOLD ↑"
        score_components["rsi"] = 2
    elif rsi_value >= state.rsi_sell:
        rsi_signal = "OVERBOUGHT ↓"
        score_components["rsi"] = -2
    elif rsi_value >= 50:
        rsi_signal = "BULLISH"
        score_components["rsi"] = 1
    else:
        rsi_signal = "BEARISH"
        score_components["rsi"] = -1
    
    # Stochastic RSI
    k_now = safe_get(last, "stoch_k")
    d_now = safe_get(last, "stoch_d")
    k_prev = safe_get(prev, "stoch_k")
    d_prev = safe_get(prev, "stoch_d")
    
    cross_up = k_prev < d_prev and k_now > d_now
    cross_down = k_prev > d_prev and k_now < d_now
    
    if k_now <= 20 and cross_up:
        stoch_signal = "OVERSOLD CROSS ↑"
        score_components["stoch"] = 2
    elif k_now >= 80 and cross_down:
        stoch_signal = "OVERBOUGHT CROSS ↓"
        score_components["stoch"] = -2
    elif k_now <= 20:
        stoch_signal = "OVERSOLD"
        score_components["stoch"] = 1
    elif k_now >= 80:
        stoch_signal = "OVERBOUGHT"
        score_components["stoch"] = -1
    elif k_now > d_now:
        stoch_signal = "BULLISH"
        score_components["stoch"] = 1
    else:
        stoch_signal = "BEARISH"
        score_components["stoch"] = -1
    
    # EMA
    prev_diff = safe_get(prev, "ema_fast") - safe_get(prev, "ema_slow")
    last_diff = safe_get(last, "ema_fast") - safe_get(last, "ema_slow")
    
    if prev_diff < 0 and last_diff > 0:
        ema_signal = "GOLDEN CROSS ✨"
        score_components["ema"] = 2
    elif prev_diff > 0 and last_diff < 0:
        ema_signal = "DEATH CROSS 💀"
        score_components["ema"] = -2
    elif last_diff > 0:
        ema_signal = "BULLISH"
        score_components["ema"] = 1
    else:
        ema_signal = "BEARISH"
        score_components["ema"] = -1
    
    # MACD
    macd_now = safe_get(last, "macd")
    signal_now = safe_get(last, "macd_signal")
    macd_prev = safe_get(prev, "macd")
    signal_prev = safe_get(prev, "macd_signal")
    
    if macd_prev < signal_prev and macd_now > signal_now:
        macd_signal_text = "BULLISH CROSS ↑"
        score_components["macd"] = 2
    elif macd_prev > signal_prev and macd_now < signal_now:
        macd_signal_text = "BEARISH CROSS ↓"
        score_components["macd"] = -2
    elif macd_now > signal_now:
        macd_signal_text = "BULLISH"
        score_components["macd"] = 1
    else:
        macd_signal_text = "BEARISH"
        score_components["macd"] = -1
    
    # Bollinger Bands
    bb_upper = safe_get(last, "bb_upper")
    bb_lower = safe_get(last, "bb_lower")
    bb_percent = (price - bb_lower) / (bb_upper - bb_lower) * 100 if bb_upper != bb_lower else 50
    
    if price <= bb_lower:
        bb_signal = "BELOW LOWER ↑"
        score_components["bb"] = 2
    elif price >= bb_upper:
        bb_signal = "ABOVE UPPER ↓"
        score_components["bb"] = -2
    elif bb_percent < 40:
        bb_signal = "LOWER HALF"
        score_components["bb"] = 1
    elif bb_percent > 60:
        bb_signal = "UPPER HALF"
        score_components["bb"] = -1
    else:
        bb_signal = "MID BAND"
        score_components["bb"] = 0
    
    # ADX
    adx_value = safe_get(last, "adx")
    plus_di = safe_get(last, "plus_di")
    minus_di = safe_get(last, "minus_di")
    is_trending = adx_value >= ADX_TRENDING
    
    if not is_trending:
        adx_signal = f"RANGING ({adx_value:.0f})"
    elif plus_di > minus_di:
        adx_signal = f"TREND UP ({adx_value:.0f})"
    else:
        adx_signal = f"TREND DOWN ({adx_value:.0f})"
    
    # OBV
    obv_now = safe_get(last, "obv")
    obv_prev = safe_get(prev, "obv")
    obv_ema = float(df["obv"].ewm(span=10).mean().iloc[-2])
    
    if obv_now > obv_ema and obv_now > obv_prev:
        obv_signal = "RISING ↑"
        score_components["obv"] = 1
    elif obv_now < obv_ema and obv_now < obv_prev:
        obv_signal = "FALLING ↓"
        score_components["obv"] = -1
    else:
        obv_signal = "NEUTRAL"
        score_components["obv"] = 0
    
    # VWAP
    vwap_value = safe_get(last, "vwap")
    if vwap_value > 0:
        if price > vwap_value * 1.003:
            vwap_signal = "ABOVE VWAP ↑"
            score_components["vwap"] = 1
        elif price < vwap_value * 0.997:
            vwap_signal = "BELOW VWAP ↓"
            score_components["vwap"] = -1
        else:
            vwap_signal = "AT VWAP"
            score_components["vwap"] = 0
    else:
        vwap_signal = "N/A"
        score_components["vwap"] = 0
    
    # Market regime (SMA200)
    sma200 = safe_get(last, "sma200")
    if price > sma200:
        regime_signal = "BULL MARKET ↑"
        score_components["regime"] = 1
    else:
        regime_signal = "BEAR MARKET ↓"
        score_components["regime"] = -1
    
    # Volume
    volume_ratio = safe_get(last, "volume_ratio", 1.0)
    raw_direction = sum(score_components.values())
    
    if volume_ratio >= VOLUME_SPIKE_MULT:
        volume_signal = f"SPIKE {volume_ratio:.1f}x ✅"
        score_components["volume"] = 1 if raw_direction >= 0 else -1
    elif volume_ratio < 0.6:
        volume_signal = f"LOW {volume_ratio:.1f}x ⚠️"
        score_components["volume"] = -1
    else:
        volume_signal = f"NORMAL {volume_ratio:.1f}x"
        score_components["volume"] = 0
    
    # Candlestick patterns
    candle_score = 0
    if safe_get(last, "hammer") > 0:
        patterns.append("Hammer 🔨")
        candle_score += 1
    
    engulfing = safe_get(last, "engulfing")
    if engulfing > 0:
        patterns.append("Bullish Engulfing 🟢")
        candle_score += 2
    elif engulfing < 0:
        patterns.append("Bearish Engulfing 🔴")
        candle_score -= 2
    
    if candle_score != 0:
        score_components["candle"] = max(-2, min(2, candle_score))
    
    # ATR for stop loss/take profit
    atr_value = safe_get(last, "atr")
    
    # MTF analysis
    mtf_note = ""
    mtf_conflict = False
    if mtf and mtf.get("bias") not in ("UNKNOWN", None):
        bias = mtf["bias"]
        current_score = sum(score_components.values())
        
        if bias == "BEARISH" and current_score > 0:
            for key in score_components:
                score_components[key] = score_components[key] // 2
            mtf_note = "⚠️ 4H BEARISH — score halved"
            mtf_conflict = True
        elif bias == "BULLISH" and current_score < 0:
            for key in score_components:
                score_components[key] = score_components[key] // 2
            mtf_note = "⚠️ 4H BULLISH — score halved"
            mtf_conflict = True
        else:
            mtf_note = f"✅ 4H confirms: {bias}"
    
    # Fear & Greed
    fg_note = ""
    fg_warn_long = False
    if fear_greed is not None and USE_FEAR_GREED:
        if fear_greed > FG_MAX_FOR_LONG:
            fg_note = f"⚠️ Extreme Greed ({fear_greed}) — be careful"
            fg_warn_long = True
        elif fear_greed < FG_MIN_FOR_LONG:
            fg_note = f"📊 Extreme Fear ({fear_greed}) — possible bottom"
        else:
            fg_note = f"📊 F&G: {fear_greed} {'(Greed)' if fear_greed >= 50 else '(Fear)'}"
    
    raw_score = sum(score_components.values())
    adjusted_score = max(-12, min(12, raw_score // 2 if not is_trending else raw_score))
    
    if adjusted_score >= 8:
        composite = "STRONG BUY"
    elif adjusted_score >= 4:
        composite = "BUY"
    elif adjusted_score <= -8:
        composite = "STRONG SELL"
    elif adjusted_score <= -4:
        composite = "SELL"
    else:
        composite = "NEUTRAL"
    
    is_long = composite in ("BUY", "STRONG BUY")
    is_short = composite in ("SELL", "STRONG SELL")
    
    sl_multiplier = config["atr_sl"]
    tp1_multiplier = config["atr_tp1"]
    tp2_multiplier = config["atr_tp2"]
    
    if is_long:
        stop_loss = price - atr_value * sl_multiplier
        take_profit1 = price + atr_value * tp1_multiplier
        take_profit2 = price + atr_value * tp2_multiplier
    else:
        stop_loss = price + atr_value * sl_multiplier
        take_profit1 = price - atr_value * tp1_multiplier
        take_profit2 = price - atr_value * tp2_multiplier
    
    risk_reward_ratio = abs(take_profit2 - price) / abs(price - stop_loss) if abs(price - stop_loss) > 0 else 0
    
    # Exit signals
    exit_signal = None
    exit_reason = None
    
    if rsi_value >= state.rsi_sell - 5 and score_components.get("macd", 0) == -2 and bb_percent > 75:
        exit_signal = "EXIT LONG 🔔"
        exit_reason = "RSI overbought + MACD bearish + Near upper BB"
    elif rsi_value <= state.rsi_buy + 5 and score_components.get("macd", 0) == 2 and bb_percent < 25:
        exit_signal = "EXIT SHORT 🔔"
        exit_reason = "RSI oversold + MACD bullish + Near lower BB"
    
    return {
        "price": price,
        "timeframe": state.active_tf,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "rsi_value": rsi_value,
        "rsi_signal": rsi_signal,
        "stoch_k": k_now,
        "stoch_d": d_now,
        "stoch_signal": stoch_signal,
        "ema_fast": safe_get(last, "ema_fast"),
        "ema_slow": safe_get(last, "ema_slow"),
        "ema_signal": ema_signal,
        "macd_value": macd_now,
        "macd_histogram": safe_get(last, "macd_histogram"),
        "macd_signal": macd_signal_text,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_percent": bb_percent,
        "bb_signal": bb_signal,
        "adx_value": adx_value,
        "adx_signal": adx_signal,
        "is_trending": is_trending,
        "obv_signal": obv_signal,
        "vwap_signal": vwap_signal,
        "volume_signal": volume_signal,
        "regime_signal": regime_signal,
        "atr_value": atr_value,
        "sma200": sma200,
        "patterns": patterns,
        "mtf": mtf,
        "mtf_note": mtf_note,
        "mtf_conflict": mtf_conflict,
        "fear_greed": fear_greed,
        "fg_note": fg_note,
        "fg_warn_long": fg_warn_long,
        "score_components": score_components,
        "raw_score": raw_score,
        "adjusted_score": adjusted_score,
        "composite": composite,
        "is_long": is_long,
        "is_short": is_short,
        "stop_loss": stop_loss,
        "take_profit1": take_profit1,
        "take_profit2": take_profit2,
        "risk_reward_ratio": risk_reward_ratio,
        "mode": state.mode,
        "exit_signal": exit_signal,
        "exit_reason": exit_reason,
    }

async def run_analysis(symbol):
    df = await fetch_ohlcv(symbol, state.active_tf)
    if df is None:
        return None
    
    df = compute_indicators(df)
    if len(df) < 3:
        return None
    
    df_4h = await fetch_ohlcv(symbol, CONFIRM_TF)
    mtf_bias = compute_mtf_bias(df_4h)
    
    fear_greed_value = await fetch_fear_greed() if USE_FEAR_GREED else None
    
    return generate_signals(df, mtf_bias, fear_greed_value)

# ══════════════════════════════════════════════════════════════════════
#  🎨  FORMATTING
# ══════════════════════════════════════════════════════════════════════

SIGNAL_ICONS = {
    "STRONG BUY": "🟢🟢",
    "BUY": "🟢",
    "NEUTRAL": "⚪️",
    "SELL": "🔴",
    "STRONG SELL": "🔴🔴"
}

MODE_ICONS = {
    "conservative": "🛡",
    "balanced": "⚖️",
    "sensitive": "⚡"
}

def get_signal_icon(composite):
    return SIGNAL_ICONS.get(composite, "⚪️")

def calculate_signal_grade(signal_data):
    """
    Grade system: S / A / B / C / D
    Based on score, MTF confirmation, volume, and candlestick patterns.
    """
    score = abs(signal_data["adjusted_score"])
    has_mtf = not signal_data.get("mtf_conflict", False)
    has_volume = signal_data.get("score_components", {}).get("volume", 0) > 0
    has_patterns = len(signal_data.get("patterns", [])) > 0
    has_candle = signal_data.get("score_components", {}).get("candle", 0) != 0
    
    bonus = sum([has_mtf, has_volume, has_patterns or has_candle])
    
    if score >= 10 and bonus >= 2:
        return "S"
    if score >= 8 and bonus >= 2:
        return "A"
    if score >= 6 and bonus >= 1:
        return "B"
    if score >= 4:
        return "C"
    return "D"

GRADE_LABELS = {
    "S": "🏆 Grade S — Perfect signal",
    "A": "⭐ Grade A — Very strong",
    "B": "✅ Grade B — Good signal",
    "C": "⚠️ Grade C — Moderate, be careful",
    "D": "❌ Grade D — Weak, avoid",
}

def create_score_bar(score, max_value=12):
    """Visual score bar with direction."""
    filled = min(abs(score), max_value)
    empty = max_value - filled
    bar = "█" * filled + "░" * empty
    arrow = "▲" if score > 0 else "▼" if score < 0 else "─"
    return f"[{bar}] {arrow}"

def get_indicator_dot(value):
    """Single indicator dot: green/red/gray."""
    if value > 0:
        return "🟢"
    if value < 0:
        return "🔴"
    return "⚪️"

def format_signal_message(symbol: str, signal_data: dict) -> str:
    """
    Redesigned signal message.
    Structure:
      1. Header — coin + composite + grade
      2. Quick summary bar
      3. Entry / SL / TP levels
      4. Indicators grouped: Trend | Momentum | Volume
      5. Filters (MTF + F&G)
      6. How to trade — step by step
      7. Footer
    """
    coin = symbol_to_coin(symbol)
    icon = get_signal_icon(signal_data["composite"])
    grade = calculate_signal_grade(signal_data)
    components = signal_data["score_components"]
    mode_icon = MODE_ICONS.get(signal_data["mode"], "")
    
    sl_percent = abs(signal_data["price"] - signal_data["stop_loss"]) / signal_data["price"] * 100
    tp1_percent = abs(signal_data["price"] - signal_data["take_profit1"]) / signal_data["price"] * 100
    tp2_percent = abs(signal_data["price"] - signal_data["take_profit2"]) / signal_data["price"] * 100
    
    # Direction block
    if signal_data["is_long"]:
        direction_header = "📈 *LONG  —  BUY now*"
        action_tip = "_Spot Buy → Set SL → Sell at TP1/TP2_"
        trade_steps = (
            f"*── How to trade ──────────*\n"
            f"1️⃣  Open Gate.io / Binance\n"
            f"2️⃣  *{coin}/USDT* Spot → BUY\n"
            f"3️⃣  Place Stop Loss at: `${signal_data['stop_loss']:,.4f}`\n"
            f"4️⃣  Sell 50% at TP1: `${signal_data['take_profit1']:,.4f}`\n"
            f"5️⃣  Keep 50% until TP2: `${signal_data['take_profit2']:,.4f}`\n"
        )
    elif signal_data["is_short"]:
        direction_header = "📉 *SHORT  —  SELL now*"
        action_tip = "_If holding, sell → re-enter lower_"
        trade_steps = (
            f"*── How to trade ──────────*\n"
            f"1️⃣  Open Gate.io / Binance\n"
            f"2️⃣  If holding {coin}, SELL\n"
            f"3️⃣  Watch SL: `${signal_data['stop_loss']:,.4f}`\n"
            f"4️⃣  TP1: `${signal_data['take_profit1']:,.4f}`\n"
            f"5️⃣  TP2: `${signal_data['take_profit2']:,.4f}`\n"
        )
    else:
        direction_header = "➡️ *NEUTRAL  —  Wait*"
        action_tip = "_Signal not strong enough yet_"
        trade_steps = "_We'll check again in next auto-scan._\n"
    
    # Score bar
    score_bar = create_score_bar(signal_data["adjusted_score"])
    
    # Indicator dots (quick visual)
    indicator_dots = (
        f"{get_indicator_dot(components.get('rsi', 0))}RSI "
        f"{get_indicator_dot(components.get('stoch', 0))}SRSI "
        f"{get_indicator_dot(components.get('ema', 0))}EMA "
        f"{get_indicator_dot(components.get('macd', 0))}MACD "
        f"{get_indicator_dot(components.get('bb', 0))}BB "
        f"{get_indicator_dot(components.get('obv', 0))}OBV "
        f"{get_indicator_dot(components.get('vwap', 0))}VWAP "
        f"{get_indicator_dot(components.get('volume', 0))}VOL"
    )
    
    # Warnings
    warnings = []
    if signal_data.get("mtf_conflict"):
        warnings.append("⚠️ *4H trend opposite* — reduce position size!")
    if signal_data.get("fg_warn_long") and signal_data["is_long"]:
        warnings.append("⚠️ *Extreme Greed* — overbought market, be careful")
    if not signal_data["is_trending"]:
        warnings.append("⚠️ *Sideways market* (ADX weak) — score halved")
    
    warning_block = ("\n" + "\n".join(warnings) + "\n") if warnings else ""
    
    # Patterns
    pattern_line = ""
    if signal_data["patterns"]:
        pattern_line = f"🕯 *Patterns:* {' · '.join(signal_data['patterns'])}\n"
    
    # Indicator detail (grouped)
    trend_block = (
        f"  {get_indicator_dot(components.get('ema', 0))} EMA{EMA_FAST}/{EMA_SLOW}  →  {signal_data['ema_signal']}\n"
        f"  {get_indicator_dot(components.get('macd', 0))} MACD       →  {signal_data['macd_signal']}\n"
        f"  {get_indicator_dot(components.get('regime', 0))} Regime     →  {signal_data['regime_signal']}\n"
        f"  {get_indicator_dot(components.get('vwap', 0))} VWAP       →  {signal_data['vwap_signal']}\n"
        f"  ◽ ADX        →  {signal_data['adx_signal']}\n"
    )
    
    momentum_block = (
        f"  {get_indicator_dot(components.get('rsi', 0))} RSI `{signal_data['rsi_value']:.1f}`  →  {signal_data['rsi_signal']}\n"
        f"  {get_indicator_dot(components.get('stoch', 0))} StochRSI K:`{signal_data['stoch_k']:.1f}` D:`{signal_data['stoch_d']:.1f}`  →  {signal_data['stoch_signal']}\n"
        f"  {get_indicator_dot(components.get('bb', 0))} BB `{signal_data['bb_percent']:.0f}%`  →  {signal_data['bb_signal']}\n"
    )
    
    volume_block = (
        f"  {get_indicator_dot(components.get('obv', 0))} OBV     →  {signal_data['obv_signal']}\n"
        f"  {get_indicator_dot(components.get('volume', 0))} Volume  →  {signal_data['volume_signal']}\n"
    )
    
    return (
        f"{icon}{icon} *{coin}/USDT* {icon}{icon}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{direction_header}\n"
        f"{action_tip}\n\n"
        
        f"*📊 Signal Grade:* `{grade}`  —  {GRADE_LABELS[grade]}\n"
        f"`{score_bar}` `{signal_data['adjusted_score']:+d}/12`\n"
        f"{indicator_dots}\n"
        f"{pattern_line}"
        f"{warning_block}\n"
        
        f"*💰 Entry & Levels*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"💵 Entry Price : `${signal_data['price']:,.4f}`\n"
        f"🛑 Stop Loss   : `${signal_data['stop_loss']:,.4f}`  _(-{sl_percent:.1f}%)_\n"
        f"🎯 TP1 (50%)   : `${signal_data['take_profit1']:,.4f}`  _(+{tp1_percent:.1f}%)_\n"
        f"🎯 TP2 (50%)   : `${signal_data['take_profit2']:,.4f}`  _(+{tp2_percent:.1f}%)_\n"
        f"📐 R:R Ratio   : `1 : {signal_data['risk_reward_ratio']:.1f}`\n"
        f"⏱  Timeframe  : `{signal_data['timeframe']}` + `{CONFIRM_TF}` MTF\n"
        f"🔒 Candle      : _Confirmed — no repaint_\n\n"
        
        f"*📈 Trend Indicators*\n"
        f"{trend_block}\n"
        f"*⚡ Momentum Indicators*\n"
        f"{momentum_block}\n"
        f"*📦 Volume Indicators*\n"
        f"{volume_block}\n"
        
        f"*🔍 Filters*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"4H MTF  :  {signal_data['mtf_note']}\n"
        f"F&G     :  {signal_data['fg_note']}\n\n"
        
        f"*🛒 Trade Steps*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{trade_steps}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{mode_icon} `{signal_data['mode'].upper()}` mode  ·  🕒 _{signal_data['timestamp']}_\n"
        f"_⚠️ Not financial advice. Do your own research._"
    )


def format_exit_message(symbol: str, signal_data: dict) -> str:
    coin = symbol_to_coin(symbol)
    is_long_exit = "LONG" in signal_data.get("exit_signal", "")
    icon = "🔴" if is_long_exit else "🟢"
    action = "SELL now (close position)" if is_long_exit else "BUY now (cover short)"
    
    return (
        f"🔔🔔 *EXIT SIGNAL — {coin}/USDT* 🔔🔔\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{icon} *{signal_data['exit_signal']}*\n\n"
        f"💵 Current Price : `${signal_data['price']:,.4f}`\n"
        f"📋 Reason: _{signal_data['exit_reason']}_\n\n"
        f"RSI: `{signal_data['rsi_value']:.1f}` · MACD: {signal_data['macd_signal']}\n"
        f"BB position: `{signal_data['bb_percent']:.0f}%` of band\n\n"
        f"*👉 Action now:* {action}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🕒 _{signal_data['timestamp']}_"
    )


def format_scan_row(symbol: str, signal_data: dict) -> str:
    coin = symbol_to_coin(symbol)
    grade = calculate_signal_grade(signal_data)
    filter_indicator = "~" if not signal_data["is_trending"] else ""
    mtf_indicator = "✅" if not signal_data.get("mtf_conflict") else "⚠️"
    pattern_indicator = "🕯" if signal_data["patterns"] else "  "
    
    return (
        f"{get_signal_icon(signal_data['composite'])} *{coin}*"
        f"  `${signal_data['price']:,.4f}`"
        f"  `{filter_indicator}{signal_data['adjusted_score']:+d}/12`"
        f"  `{grade}`"
        f"  {pattern_indicator}{mtf_indicator}"
    )


def format_price_alert(symbol: str, current_price: float, target_price: float, direction: str, signal_data: dict = None) -> str:
    """Rich price alert with optional signal context."""
    coin = symbol_to_coin(symbol)
    arrow = "🚀" if direction == "above" else "📉"
    percent_change = abs(current_price - target_price) / target_price * 100
    
    signal_note = ""
    if signal_data and signal_data.get("composite") != "NEUTRAL":
        grade = calculate_signal_grade(signal_data)
        signal_note = (
            f"\n\n*📊 Current Signal:*\n"
            f"{get_signal_icon(signal_data['composite'])} {signal_data['composite']}  `Grade {grade}`  `{signal_data['adjusted_score']:+d}/12`\n"
            f"SL: `${signal_data['stop_loss']:,.4f}` · TP2: `${signal_data['take_profit2']:,.4f}`\n"
            f"_/analyze {coin} to see full signal_"
        )
    
    return (
        f"{arrow}{arrow} *PRICE ALERT — {coin}/USDT* {arrow}{arrow}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"Current : `${current_price:,.4f}`\n"
        f"Target  : `${target_price:,.2f}` ({direction})\n"
        f"Moved   : `{percent_change:.1f}%` from target\n"
        f"{signal_note}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🕒 _{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_"
    )


def format_scan_summary(rows: list, signals: list, timeframe: str, mode: str) -> str:
    """Full scan summary with grouped BUY/SELL/NEUTRAL."""
    buy_signals = [(s, sig) for s, sig in signals if sig["is_long"]]
    sell_signals = [(s, sig) for s, sig in signals if sig["is_short"]]
    mode_icon = MODE_ICONS.get(mode, "")
    
    header = (
        f"📡 *MARKET SCAN*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"⏱ `{timeframe.upper()}` · {mode_icon}`{mode.upper()}` · "
        f"🕒 _{datetime.utcnow().strftime('%d %b %H:%M UTC')}_\n\n"
    )
    
    body = "\n".join(rows) + "\n"
    
    footer = f"\n🟢 *{len(buy_signals)} BUY*  ·  🔴 *{len(sell_signals)} SELL*"
    if not signals:
        footer += "\n_No actionable signals — wait._"
    else:
        footer += "\n_Check full signals below and trade manually._"
    
    return header + body + footer

# ══════════════════════════════════════════════════════════════════════
#  🤖  COMMANDS
# ══════════════════════════════════════════════════════════════════════

HELP_TEXT = f"""
📡 *Crypto Signal Bot — Manual Trading*
_See signals → Trade manually on Gate / Binance_

*📊 Analysis*
`/analyze BTC` — full signal + grade + trade steps
`/scan` — scan all coins (grouped by BUY/SELL)
`/tf 1h` — change timeframe _(1m 5m 15m 1h 4h 1d)_

*🎛 Signal Mode*
`/mode sensitive` ⚡ — more signals
`/mode balanced` ⚖️ — recommended
`/mode conservative` 🛡 — fewer but accurate

*📋 Watchlist*
`/watch` · `/add SOL` · `/remove SOL`

*🔔 Price Alert*
`/alert BTC 70000 above` — notify when price hits
`/alert ETH 2500 below`
`/alerts` — all active alerts
`/cancelalert BTC`

*⚙️ Settings*
`/threshold 25 75` — adjust RSI levels

*📐 Grade System*
`S` 🏆 Perfect · `A` ⭐ Very strong
`B` ✅ Good · `C` ⚠️ Moderate · `D` ❌ Weak

💡 _Just type_ `BTC` _to get signal!_
⏰ _Auto scan: {' · '.join(SCAN_TIMES)} UTC_
_Primary TF: {PRIMARY_TF} · Confirm TF: {CONFIRM_TF}_
"""

async def start_command(update, context):
    keyboard = [[
        InlineKeyboardButton("📊 BTC Signal", callback_data="quick_BTC"),
        InlineKeyboardButton("📡 Scan All", callback_data="scan"),
    ],[
        InlineKeyboardButton("📋 Watchlist", callback_data="watch"),
        InlineKeyboardButton("🔔 Alerts", callback_data="alerts"),
    ],[
        InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
        InlineKeyboardButton("❓ Help", callback_data="help"),
    ]]
    
    await update.message.reply_text(
        f"📡 *Crypto Signal Bot*\n"
        f"_See signals → trade manually_\n\n"
        f"Mode   : {MODE_ICONS.get(state.mode, '')} `{state.mode.upper()}`\n"
        f"TF     : `{state.active_tf}` + `{CONFIRM_TF}` MTF\n"
        f"Coins  : `{len(state.watchlist)}`\n"
        f"Grades : S 🏆 A ⭐ B ✅ C ⚠️ D ❌\n\n"
        f"_Just type BTC to get signal!_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def help_command(update, context):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

async def analyze_command(update, context):
    if not context.args:
        await update.message.reply_text("Usage: `/analyze BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    
    coin = context.args[0].upper().replace("USDT", "").strip("/")
    if not is_valid_coin(coin):
        await update.message.reply_text("❌ Invalid coin symbol.", parse_mode=ParseMode.MARKDOWN)
        return
    
    symbol = coin_to_symbol(coin)
    waiting_message = await update.message.reply_text(f"⏳ Analyzing *{coin}*...", parse_mode=ParseMode.MARKDOWN)
    
    signal = await run_analysis(symbol)
    if not signal:
        await waiting_message.edit_text(f"❌ Could not fetch data for `{coin}`.", parse_mode=ParseMode.MARKDOWN)
        return
    
    keyboard = [[
        InlineKeyboardButton("🔄 Refresh", callback_data=f"quick_{coin}"),
        InlineKeyboardButton("🔔 Alert", callback_data=f"alertmenu_{coin}")
    ]]
    
    await waiting_message.edit_text(
        format_signal_message(symbol, signal),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    if signal.get("exit_signal"):
        await update.message.reply_text(
            format_exit_message(symbol, signal),
            parse_mode=ParseMode.MARKDOWN
        )

async def scan_command(update, context):
    if not state.watchlist:
        await update.message.reply_text("📋 Watchlist empty! Use `/add BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    
    waiting_message = await update.message.reply_text(
        f"🔄 Scanning *{len(state.watchlist)} coins*...\n_This may take a moment_",
        parse_mode=ParseMode.MARKDOWN,
    )
    
    min_score = MODES[state.mode]["min_score"]
    rows = []
    actionable_signals = []
    
    for symbol in state.watchlist:
        signal = await run_analysis(symbol)
        if not signal:
            rows.append(f"⚠️ `{symbol_to_coin(symbol)}` — data error")
            continue
        
        rows.append(format_scan_row(symbol, signal))
        
        if abs(signal["adjusted_score"]) >= min_score and signal["composite"] != "NEUTRAL":
            actionable_signals.append((symbol, signal))
    
    # Sort signals: STRONG first, then by score
    actionable_signals.sort(key=lambda x: abs(x[1]["adjusted_score"]), reverse=True)
    
    summary = format_scan_summary(rows, actionable_signals, state.active_tf, state.mode)
    await waiting_message.edit_text(summary, parse_mode=ParseMode.MARKDOWN)
    
    # Send full signal for each actionable coin
    for symbol, signal in actionable_signals:
        await update.message.reply_text(
            format_signal_message(symbol, signal),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data=f"quick_{symbol_to_coin(symbol)}"),
                InlineKeyboardButton("🔔 Alert", callback_data=f"alertmenu_{symbol_to_coin(symbol)}"),
            ]])
        )
        
        if signal.get("exit_signal"):
            await update.message.reply_text(
                format_exit_message(symbol, signal),
                parse_mode=ParseMode.MARKDOWN
            )

async def mode_command(update, context):
    if not context.args or context.args[0].lower() not in MODES:
        lines = ["*Signal Modes:*\n"]
        for name, config in MODES.items():
            active_marker = " ← active" if name == state.mode else ""
            lines.append(
                f"{MODE_ICONS.get(name, '')} *{name.upper()}*{active_marker}\n"
                f"  Score≥`{config['min_score']}/12` SL:`{config['atr_sl']}×ATR`"
            )
        await update.message.reply_text(
            "\n".join(lines) + "\n\nChange mode: `/mode sensitive`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    state.mode = context.args[0].lower()
    state.save()
    await update.message.reply_text(
        f"✅ Mode changed to {MODE_ICONS.get(state.mode, '')} *{state.mode.upper()}*",
        parse_mode=ParseMode.MARKDOWN
    )

async def timeframe_command(update, context):
    valid_timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
    
    if not context.args or context.args[0] not in valid_timeframes:
        await update.message.reply_text(
            f"Usage: `/tf 1h`\nOptions: `{'` · `'.join(valid_timeframes)}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    state.active_tf = context.args[0]
    state.save()
    await update.message.reply_text(
        f"✅ Timeframe changed to `{state.active_tf}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def watchlist_command(update, context):
    if not state.watchlist:
        await update.message.reply_text("📋 Watchlist is empty.", parse_mode=ParseMode.MARKDOWN)
        return
    
    coins = "  ·  ".join(f"`{symbol_to_coin(s)}`" for s in state.watchlist)
    await update.message.reply_text(
        f"📋 *Watchlist ({len(state.watchlist)}/{MAX_WATCHLIST})*\n\n{coins}",
        parse_mode=ParseMode.MARKDOWN
    )

async def add_command(update, context):
    if not context.args:
        await update.message.reply_text("Usage: `/add BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    
    coin = context.args[0].upper().replace("USDT", "").strip("/")
    if not is_valid_coin(coin):
        await update.message.reply_text("❌ Invalid coin symbol.", parse_mode=ParseMode.MARKDOWN)
        return
    
    symbol = coin_to_symbol(coin)
    
    if symbol in state.watchlist:
        await update.message.reply_text(f"`{coin}` already in watchlist.", parse_mode=ParseMode.MARKDOWN)
        return
    
    if len(state.watchlist) >= MAX_WATCHLIST:
        await update.message.reply_text(f"❌ Watchlist full (max {MAX_WATCHLIST}).", parse_mode=ParseMode.MARKDOWN)
        return
    
    state.watchlist.append(symbol)
    state.save()
    await update.message.reply_text(f"✅ `{coin}` added to watchlist!", parse_mode=ParseMode.MARKDOWN)

async def remove_command(update, context):
    if not context.args:
        await update.message.reply_text("Usage: `/remove BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    
    symbol = coin_to_symbol(context.args[0].upper().replace("USDT", "").strip("/"))
    
    if symbol not in state.watchlist:
        await update.message.reply_text("Coin not found in watchlist.", parse_mode=ParseMode.MARKDOWN)
        return
    
    state.watchlist.remove(symbol)
    state.save()
    await update.message.reply_text("🗑 Removed from watchlist.", parse_mode=ParseMode.MARKDOWN)

async def alert_command(update, context):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/alert BTC 70000 above`\n`/alert ETH 2500 below`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    coin = context.args[0].upper().replace("USDT", "").strip("/")
    if not is_valid_coin(coin):
        return
    
    symbol = coin_to_symbol(coin)
    
    try:
        target_price = float(context.args[1].replace(",", ""))
        assert target_price > 0
    except:
        await update.message.reply_text("❌ Please provide a valid price.", parse_mode=ParseMode.MARKDOWN)
        return
    
    direction = context.args[2].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("Please specify `above` or `below`.", parse_mode=ParseMode.MARKDOWN)
        return
    
    # Check if alert already exists
    for alert in state.price_alerts[symbol]:
        if alert["target"] == target_price and alert["direction"] == direction:
            await update.message.reply_text("⚠️ Alert already exists.", parse_mode=ParseMode.MARKDOWN)
            return
    
    state.price_alerts[symbol].append({"target": target_price, "direction": direction})
    state.save()
    
    direction_icon = "📈" if direction == "above" else "📉"
    await update.message.reply_text(
        f"🔔 {direction_icon} Alert set for *{coin}* {direction} `${target_price:,.2f}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def alerts_command(update, context):
    active_alerts = {symbol: alerts for symbol, alerts in state.price_alerts.items() if alerts}
    
    if not active_alerts:
        await update.message.reply_text("No active alerts.", parse_mode=ParseMode.MARKDOWN)
        return
    
    lines = ["🔔 *Active Alerts*\n"]
    for symbol, alert_list in active_alerts.items():
        for alert in alert_list:
            direction_icon = "📈" if alert["direction"] == "above" else "📉"
            lines.append(
                f"{direction_icon} *{symbol_to_coin(symbol)}* {alert['direction']} `${alert['target']:,.2f}`"
            )
    
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cancel_alert_command(update, context):
    if not context.args:
        await update.message.reply_text("Usage: `/cancelalert BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    
    symbol = coin_to_symbol(context.args[0].upper().replace("USDT", "").strip("/"))
    
    if state.price_alerts.get(symbol):
        state.price_alerts[symbol].clear()
        state.save()
        await update.message.reply_text("✅ All alerts for this coin removed.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No alerts found for this coin.", parse_mode=ParseMode.MARKDOWN)

async def threshold_command(update, context):
    if len(context.args) < 2:
        await update.message.reply_text(
            f"Current RSI: BUY < `{state.rsi_buy}` · SELL > `{state.rsi_sell}`\n"
            f"Change: `/threshold 25 75`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        buy_level = int(context.args[0])
        sell_level = int(context.args[1])
        assert 0 < buy_level < sell_level < 100
        
        state.rsi_buy = buy_level
        state.rsi_sell = sell_level
        state.save()
        
        await update.message.reply_text(
            f"✅ RSI thresholds updated: 🟢 < `{buy_level}` 🔴 > `{sell_level}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        await update.message.reply_text(
            "❌ Invalid values. Example: `/threshold 25 75`",
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_text_message(update, context):
    raw_text = update.message.text.strip().upper().replace("USDT", "").strip("/")
    
    if is_valid_coin(raw_text):
        context.args = [raw_text]
        await analyze_command(update, context)
    else:
        await update.message.reply_text(
            "Type a coin symbol like `BTC` or use `/help`",
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_button_callbacks(update, context):
    query = update.callback_query
    data = query.data or ""
    await query.answer()
    
    if data.startswith("quick_"):
        coin = data[6:].upper()
        if not is_valid_coin(coin):
            return
        
        waiting_message = await query.message.reply_text(
            f"⏳ Analyzing *{coin}*...",
            parse_mode=ParseMode.MARKDOWN
        )
        
        signal = await run_analysis(coin_to_symbol(coin))
        if not signal:
            await waiting_message.edit_text("❌ Error fetching data.", parse_mode=ParseMode.MARKDOWN)
            return
        
        keyboard = [[
            InlineKeyboardButton("🔄 Refresh", callback_data=f"quick_{coin}"),
            InlineKeyboardButton("🔔 Alert", callback_data=f"alertmenu_{coin}")
        ]]
        
        await waiting_message.edit_text(
            format_signal_message(coin_to_symbol(coin), signal),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        if signal.get("exit_signal"):
            await query.message.reply_text(
                format_exit_message(coin_to_symbol(coin), signal),
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif data == "scan":
        update.message = query.message
        await scan_command(update, context)
    
    elif data == "watch":
        update.message = query.message
        await watchlist_command(update, context)
    
    elif data == "alerts":
        update.message = query.message
        await alerts_command(update, context)
    
    elif data == "help":
        await query.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "settings":
        config = MODES[state.mode]
        await query.message.reply_text(
            f"⚙️ *Settings*\n"
            f"Mode: {MODE_ICONS.get(state.mode, '')} `{state.mode.upper()}`\n"
            f"TF: `{state.active_tf}` + `{CONFIRM_TF}` MTF\n"
            f"RSI: BUY < `{state.rsi_buy}` SELL > `{state.rsi_sell}`\n"
            f"Min score: `{config['min_score']}/12`\n"
            f"SL: `{config['atr_sl']}×ATR` TP1: `{config['atr_tp1']}×ATR` TP2: `{config['atr_tp2']}×ATR`",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("alertmenu_"):
        coin = data[10:].upper()
        if not is_valid_coin(coin):
            return
        
        current_price = await fetch_price(coin_to_symbol(coin))
        price_hint = f"`${current_price:,.4f}`" if current_price else "N/A"
        
        # Suggest 5% above/below current price
        if current_price:
            above_target = int(current_price * 1.05)
            below_target = int(current_price * 0.95)
        else:
            above_target = "TARGET"
            below_target = "TARGET"
        
        await query.message.reply_text(
            f"🔔 *{coin} Alert*\nCurrent: {price_hint}\n\n"
            f"`/alert {coin} {above_target} above` _(+5%)_\n"
            f"`/alert {coin} {below_target} below` _(-5%)_",
            parse_mode=ParseMode.MARKDOWN
        )

# ══════════════════════════════════════════════════════════════════════
#  ⏰  SCHEDULED JOBS
# ══════════════════════════════════════════════════════════════════════

async def auto_scan_job(bot):
    if not state.watchlist:
        return
    
    log.info("Running auto scan...")
    min_score = MODES[state.mode]["min_score"]
    rows = []
    actionable_signals = []
    
    for symbol in state.watchlist:
        signal = await run_analysis(symbol)
        if not signal:
            continue
        
        rows.append(format_scan_row(symbol, signal))
        
        if abs(signal["adjusted_score"]) >= min_score and signal["composite"] != "NEUTRAL":
            actionable_signals.append((symbol, signal))
    
    actionable_signals.sort(key=lambda x: abs(x[1]["adjusted_score"]), reverse=True)
    
    summary = format_scan_summary(rows, actionable_signals, state.active_tf, state.mode)
    await bot.send_message(CHAT_ID, summary, parse_mode=ParseMode.MARKDOWN)
    
    for symbol, signal in actionable_signals:
        await bot.send_message(
            CHAT_ID,
            format_signal_message(symbol, signal),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data=f"quick_{symbol_to_coin(symbol)}"),
                InlineKeyboardButton("🔔 Alert", callback_data=f"alertmenu_{symbol_to_coin(symbol)}"),
            ]])
        )
        
        if signal.get("exit_signal"):
            await bot.send_message(
                CHAT_ID,
                format_exit_message(symbol, signal),
                parse_mode=ParseMode.MARKDOWN
            )
    
    log.info(f"Auto scan complete — {len(actionable_signals)} signal(s).")


async def check_alerts_job(bot):
    for symbol, alert_list in list(state.price_alerts.items()):
        if not alert_list:
            continue
        
        current_price = await fetch_price(symbol)
        if current_price is None:
            continue
        
        coin = symbol_to_coin(symbol)
        changed = False
        
        for alert in list(alert_list):
            hit_alert = (
                (alert["direction"] == "above" and current_price >= alert["target"]) or
                (alert["direction"] == "below" and current_price <= alert["target"])
            )
            
            if hit_alert:
                # Get current signal for context
                signal = await run_analysis(symbol)
                alert_message = format_price_alert(symbol, current_price, alert["target"], alert["direction"], signal)
                await bot.send_message(CHAT_ID, alert_message, parse_mode=ParseMode.MARKDOWN)
                alert_list.remove(alert)
                changed = True
        
        if changed:
            state.save()

# ══════════════════════════════════════════════════════════════════════
#  ⏰  PURE ASYNCIO SCHEDULER (no apscheduler needed)
# ══════════════════════════════════════════════════════════════════════

async def scheduler_loop(bot: Bot):
    """
    Runs forever in the background.
    Checks every minute if it's time to scan or check alerts.
    No external scheduler library needed.
    """
    last_alert_check = datetime.utcnow()
    log.info(f"⏰ Scheduler running — Scans: {SCAN_TIMES} UTC, Alerts every {ALERT_CHECK_MINS}m")
    
    while True:
        await asyncio.sleep(30)  # Check every 30 seconds
        now = datetime.utcnow()
        
        # Check price alerts
        minutes_since_last_check = (now - last_alert_check).total_seconds() / 60
        if minutes_since_last_check >= ALERT_CHECK_MINS:
            last_alert_check = now
            try:
                await check_alerts_job(bot)
            except Exception as e:
                log.error(f"Alert check error: {e}")
        
        # Check scan schedule
        current_time_str = now.strftime("%H:%M")
        if current_time_str in SCAN_TIMES and now.second < 30:
            try:
                await auto_scan_job(bot)
                await asyncio.sleep(61)  # Prevent double-trigger within same minute
            except Exception as e:
                log.error(f"Auto scan error: {e}")

# ══════════════════════════════════════════════════════════════════════
#  🚀  MAIN
# ══════════════════════════════════════════════════════════════════════

async def post_initialization(app):
    asyncio.create_task(scheduler_loop(app.bot))
    log.info(f"✅ Scheduler started — Scan times: {SCAN_TIMES} UTC")

async def pre_shutdown(app):
    await exchange.close()
    log.info("Exchange connection closed.")

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("\n❌ Please set your BOT_TOKEN and CHAT_ID in the config section!\n")
        return
    
    if not isinstance(CHAT_ID, int) or CHAT_ID <= 0:
        print("\n❌ CHAT_ID must be a positive integer!\n")
        return
    
    state.load()
    log.info(f"📡 Signal Bot — {state.mode.upper()} mode · TF={state.active_tf} · {len(state.watchlist)} coins")
    
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_initialization)
           .post_shutdown(pre_shutdown)
           .build())
    
    # Register command handlers
    commands = [
        ("start", start_command),
        ("help", help_command),
        ("analyze", analyze_command),
        ("scan", scan_command),
        ("mode", mode_command),
        ("tf", timeframe_command),
        ("watch", watchlist_command),
        ("add", add_command),
        ("remove", remove_command),
        ("alert", alert_command),
        ("alerts", alerts_command),
        ("cancelalert", cancel_alert_command),
        ("threshold", threshold_command),
    ]
    
    for cmd_name, handler_func in commands:
        app.add_handler(CommandHandler(cmd_name, handler_func))
    
    app.add_handler(CallbackQueryHandler(handle_button_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    log.info("✅ Bot is now live!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
