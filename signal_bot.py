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
#  ⚙️  CONFIG — শুধু এই ২টা line edit করো
# ══════════════════════════════════════════════════════════════════════

BOT_TOKEN = "8436225888:AAFjWIQgLH4vhsgtHCVJW4ReMEmtUcxNUDc"   # @BotFather থেকে নাও
CHAT_ID   = 6537100940               # @userinfobot থেকে নাও (integer)

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
def s2coin(s):        return s.replace("/USDT","")
def coin2s(c):
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
        except Exception as e: log.error(f"Save: {e}")

    def load(self):
        try:
            if not Path(STATE_FILE).exists():
                self.watchlist = list(DEFAULT_WATCHLIST); return
            d = json.loads(Path(STATE_FILE).read_text())
            self.watchlist    = d.get("watchlist",    list(DEFAULT_WATCHLIST))
            self.active_tf    = d.get("active_tf",    PRIMARY_TF)
            self.mode         = d.get("mode",         DEFAULT_MODE)
            self.rsi_buy      = d.get("rsi_buy",      RSI_BUY)
            self.rsi_sell     = d.get("rsi_sell",     RSI_SELL)
            self.price_alerts = defaultdict(list, d.get("price_alerts", {}))
            log.info(f"State loaded — {len(self.watchlist)} coins")
        except Exception as e:
            log.error(f"Load: {e}")
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
        if not raw or len(raw) < 60: return None
        df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        df.set_index("ts", inplace=True)
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        log.error(f"OHLCV [{symbol}]: {e}"); return None

async def fetch_price(symbol: str) -> Optional[float]:
    try:
        t = await exchange.fetch_ticker(symbol)
        return float(t["last"])
    except Exception as e:
        log.error(f"Price [{symbol}]: {e}"); return None

async def fetch_fear_greed() -> Optional[int]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                data = await r.json()
                return int(data["data"][0]["value"])
    except Exception: return None

# ══════════════════════════════════════════════════════════════════════
#  📊  INDICATORS — pure pandas/numpy (pandas_ta নেই)
# ══════════════════════════════════════════════════════════════════════

def calc_rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(com=period-1, min_periods=period).mean()
    al    = loss.ewm(com=period-1, min_periods=period).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_ema(close, period):
    return close.ewm(span=period, adjust=False).mean()

def calc_sma(close, period):
    return close.rolling(period).mean()

def calc_macd(close, fast=12, slow=26, signal=9):
    macd  = calc_ema(close,fast) - calc_ema(close,slow)
    sig   = calc_ema(macd,signal)
    return macd, sig, macd - sig

def calc_bbands(close, period=20, std=2.0):
    mid   = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    return mid + std*sigma, mid, mid - std*sigma

def calc_atr(high, low, close, period=14):
    tr = pd.concat([high-low,
                    (high-close.shift()).abs(),
                    (low -close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period-1, min_periods=period).mean()

def calc_adx(high, low, close, period=14):
    up    = high.diff()
    down  = -low.diff()
    plus  = pd.Series(np.where((up>down)&(up>0),  up,   0.0), index=close.index)
    minus = pd.Series(np.where((down>up)&(down>0), down, 0.0), index=close.index)
    atr_s = calc_atr(high, low, close, period)
    dip   = 100 * calc_ema(plus,  period) / atr_s.replace(0,np.nan)
    dim   = 100 * calc_ema(minus, period) / atr_s.replace(0,np.nan)
    dx    = 100 * (dip-dim).abs() / (dip+dim).replace(0,np.nan)
    return calc_ema(dx,period), dip, dim

def calc_stoch_rsi(close, rsi_p=14, stoch_p=14, k=3, d=3):
    rsi  = calc_rsi(close, rsi_p)
    lo   = rsi.rolling(stoch_p).min()
    hi   = rsi.rolling(stoch_p).max()
    st   = 100*(rsi-lo)/(hi-lo).replace(0,np.nan)
    k_l  = st.rolling(k).mean()
    d_l  = k_l.rolling(d).mean()
    return k_l, d_l

def calc_obv(close, volume):
    return (np.sign(close.diff()).fillna(0)*volume).cumsum()

def calc_vwap(high, low, close, volume, period=20):
    tp = (high+low+close)/3
    return (tp*volume).rolling(period).sum() / volume.rolling(period).sum()

def calc_hammer(open_, high, low, close):
    body  = (close-open_).abs()
    lower = open_.combine(close,min) - low
    upper = high - open_.combine(close,max)
    return ((lower>=2*body) & (upper<=body*0.5) & (body>0)).astype(int)

def calc_engulfing(open_, close):
    po = open_.shift(1); pc = close.shift(1)
    bull = (pc<po) & (close>po) & (open_<pc)
    bear = (pc>po) & (close<po) & (open_>pc)
    r = pd.Series(0, index=close.index)
    r[bull]=1; r[bear]=-1
    return r

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c,h,l,o,v = df["close"],df["high"],df["low"],df["open"],df["volume"]
    df["rsi"]       = calc_rsi(c, RSI_PERIOD)
    df["ema_fast"]  = calc_ema(c, EMA_FAST)
    df["ema_slow"]  = calc_ema(c, EMA_SLOW)
    df["sma200"]    = calc_sma(c, SMA_TREND)
    df["atr"]       = calc_atr(h,l,c, ATR_PERIOD)
    df["obv"]       = calc_obv(c,v)
    df["macd"], df["macd_sig"], df["macd_hist"] = calc_macd(c,MACD_FAST,MACD_SLOW,MACD_SIGNAL)
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = calc_bbands(c,BB_PERIOD,BB_STD)
    df["adx"], df["dmp"], df["dmn"]              = calc_adx(h,l,c,ADX_PERIOD)
    df["stoch_k"], df["stoch_d"]                 = calc_stoch_rsi(c,RSI_PERIOD,14,3,3)
    df["vwap"]      = calc_vwap(h,l,c,v,VWAP_PERIOD)
    df["vol_avg"]   = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_avg"].clip(lower=0.001)
    df["cdl_hammer"]    = calc_hammer(o,h,l,c)
    df["cdl_engulfing"] = calc_engulfing(o,c)
    df.dropna(inplace=True)
    return df

def _v(row, col, default=0.0):
    try:
        x = float(row[col]) if col in row.index else default
        return x if pd.notna(x) else default
    except: return default

def compute_mtf_bias(df4h):
    if df4h is None or len(df4h)<3: return {"bias":"UNKNOWN","score":0}
    df4h = compute_indicators(df4h)
    if len(df4h)<3: return {"bias":"UNKNOWN","score":0}
    last=df4h.iloc[-2]; score=0; price=float(last["close"])
    if _v(last,"ema_fast")>_v(last,"ema_slow"): score+=2
    else: score-=2
    r4=_v(last,"rsi")
    if r4>55: score+=1
    elif r4<45: score-=1
    if _v(last,"macd")>_v(last,"macd_sig"): score+=1
    else: score-=1
    if price>_v(last,"sma200"): score+=1
    else: score-=1
    bias = "BULLISH" if score>=3 else "BEARISH" if score<=-3 else "NEUTRAL"
    return {"bias":bias,"score":score}

def generate_signals(df, mtf=None, fg=None):
    if len(df)<4: return {}
    last=df.iloc[-2]; prev=df.iloc[-3]
    price=float(last["close"]); cfg=MODES[state.mode]
    sc={}; patterns=[]

    rsi=_v(last,"rsi")
    if rsi<=state.rsi_buy:    rsi_sig,sc["rsi"]="OVERSOLD ↑",+2
    elif rsi>=state.rsi_sell: rsi_sig,sc["rsi"]="OVERBOUGHT ↓",-2
    elif rsi>=50:              rsi_sig,sc["rsi"]="BULLISH",+1
    else:                      rsi_sig,sc["rsi"]="BEARISH",-1

    k_n,d_n=_v(last,"stoch_k"),_v(last,"stoch_d")
    k_p,d_p=_v(prev,"stoch_k"),_v(prev,"stoch_d")
    cup=k_p<d_p and k_n>d_n; cdn=k_p>d_p and k_n<d_n
    if k_n<=20 and cup:    stoch_sig,sc["stoch"]="OVERSOLD CROSS ↑",+2
    elif k_n>=80 and cdn:  stoch_sig,sc["stoch"]="OVERBOUGHT CROSS ↓",-2
    elif k_n<=20:           stoch_sig,sc["stoch"]="OVERSOLD",+1
    elif k_n>=80:           stoch_sig,sc["stoch"]="OVERBOUGHT",-1
    elif k_n>d_n:           stoch_sig,sc["stoch"]="BULLISH",+1
    else:                   stoch_sig,sc["stoch"]="BEARISH",-1

    pd_=_v(prev,"ema_fast")-_v(prev,"ema_slow")
    ld_=_v(last,"ema_fast")-_v(last,"ema_slow")
    if pd_<0 and ld_>0:    ema_sig,sc["ema"]="GOLDEN CROSS ✨",+2
    elif pd_>0 and ld_<0:   ema_sig,sc["ema"]="DEATH CROSS 💀",-2
    elif ld_>0:              ema_sig,sc["ema"]="BULLISH",+1
    else:                    ema_sig,sc["ema"]="BEARISH",-1

    m_n,s_n,h_n=_v(last,"macd"),_v(last,"macd_sig"),_v(last,"macd_hist")
    m_p,s_p=_v(prev,"macd"),_v(prev,"macd_sig")
    if m_p<s_p and m_n>s_n:  macd_sig,sc["macd"]="BULLISH CROSS ↑",+2
    elif m_p>s_p and m_n<s_n: macd_sig,sc["macd"]="BEARISH CROSS ↓",-2
    elif m_n>s_n:              macd_sig,sc["macd"]="BULLISH",+1
    else:                      macd_sig,sc["macd"]="BEARISH",-1

    bb_u=_v(last,"bb_upper"); bb_l=_v(last,"bb_lower")
    bb_pct=(price-bb_l)/(bb_u-bb_l)*100 if bb_u!=bb_l else 50
    if price<=bb_l:       bb_sig,sc["bb"]="BELOW LOWER ↑",+2
    elif price>=bb_u:      bb_sig,sc["bb"]="ABOVE UPPER ↓",-2
    elif bb_pct<40:        bb_sig,sc["bb"]="LOWER HALF",+1
    elif bb_pct>60:        bb_sig,sc["bb"]="UPPER HALF",-1
    else:                  bb_sig,sc["bb"]="MID BAND",0

    adx_v=_v(last,"adx"); dmp=_v(last,"dmp"); dmn=_v(last,"dmn")
    trending=adx_v>=ADX_TRENDING
    adx_sig=(f"RANGING ({adx_v:.0f})" if not trending else
             f"TREND UP ({adx_v:.0f})" if dmp>dmn else f"TREND DOWN ({adx_v:.0f})")

    obv_n=_v(last,"obv"); obv_p=_v(prev,"obv")
    obv_ema=float(df["obv"].ewm(span=10).mean().iloc[-2])
    if obv_n>obv_ema and obv_n>obv_p:   obv_sig,sc["obv"]="RISING ↑",+1
    elif obv_n<obv_ema and obv_n<obv_p:  obv_sig,sc["obv"]="FALLING ↓",-1
    else:                                 obv_sig,sc["obv"]="NEUTRAL",0

    vwap=_v(last,"vwap")
    if vwap>0:
        if price>vwap*1.003:    vwap_sig,sc["vwap"]="ABOVE VWAP ↑",+1
        elif price<vwap*0.997:  vwap_sig,sc["vwap"]="BELOW VWAP ↓",-1
        else:                    vwap_sig,sc["vwap"]="AT VWAP",0
    else: vwap_sig="N/A"; sc["vwap"]=0

    sma200=_v(last,"sma200")
    if price>sma200: regime_sig,sc["regime"]="BULL MARKET ↑",+1
    else:             regime_sig,sc["regime"]="BEAR MARKET ↓",-1

    vol_r=_v(last,"vol_ratio",1.0)
    raw_dir=sum(sc.values())
    if vol_r>=VOLUME_SPIKE_MULT:   vol_sig,sc["vol"]=f"SPIKE {vol_r:.1f}x ✅",+1 if raw_dir>=0 else -1
    elif vol_r<0.6:                 vol_sig,sc["vol"]=f"LOW {vol_r:.1f}x ⚠️",-1
    else:                           vol_sig,sc["vol"]=f"NORMAL {vol_r:.1f}x",0

    cdl=0
    if _v(last,"cdl_hammer")>0:    patterns.append("Hammer 🔨"); cdl+=1
    eng=_v(last,"cdl_engulfing")
    if eng>0:   patterns.append("Bullish Engulfing 🟢"); cdl+=2
    elif eng<0: patterns.append("Bearish Engulfing 🔴"); cdl-=2
    if cdl!=0: sc["candle"]=max(-2,min(2,cdl))

    atr=_v(last,"atr")

    mtf_note=""; mtf_conflict=False
    if mtf and mtf.get("bias") not in ("UNKNOWN",None):
        bias=mtf["bias"]; raw_now=sum(sc.values())
        if bias=="BEARISH" and raw_now>0:
            for k in sc: sc[k]=sc[k]//2
            mtf_note="⚠️ 4H BEARISH — score halved"; mtf_conflict=True
        elif bias=="BULLISH" and raw_now<0:
            for k in sc: sc[k]=sc[k]//2
            mtf_note="⚠️ 4H BULLISH — score halved"; mtf_conflict=True
        else: mtf_note=f"✅ 4H confirms: {bias}"

    fg_note=""; fg_warn_long=False
    if fg is not None and USE_FEAR_GREED:
        if fg>FG_MAX_FOR_LONG:   fg_note=f"⚠️ Extreme Greed ({fg}) — সতর্ক থাকো"; fg_warn_long=True
        elif fg<FG_MIN_FOR_LONG: fg_note=f"📊 Extreme Fear ({fg}) — possible bottom"
        else:                     fg_note=f"📊 F&G: {fg} {'(Greed)' if fg>=50 else '(Fear)'}"

    raw_score=sum(sc.values())
    adx_filt=not trending
    comp=max(-12,min(12,raw_score//2 if adx_filt else raw_score))

    if comp>=8:    composite="STRONG BUY"
    elif comp>=4:  composite="BUY"
    elif comp<=-8: composite="STRONG SELL"
    elif comp<=-4: composite="SELL"
    else:          composite="NEUTRAL"

    is_long=composite in ("BUY","STRONG BUY")
    is_short=composite in ("SELL","STRONG SELL")

    sl_m=cfg["atr_sl"]; tp1_m=cfg["atr_tp1"]; tp2_m=cfg["atr_tp2"]
    if is_long:
        sl=price-atr*sl_m; tp1=price+atr*tp1_m; tp2=price+atr*tp2_m
    else:
        sl=price+atr*sl_m; tp1=price-atr*tp1_m; tp2=price-atr*tp2_m

    rr2=abs(tp2-price)/abs(price-sl) if abs(price-sl)>0 else 0

    exit_sig=None; exit_rsn=None
    if rsi>=state.rsi_sell-5 and sc.get("macd",0)==-2 and bb_pct>75:
        exit_sig="EXIT LONG 🔔"; exit_rsn="RSI overbought + MACD bearish + Near upper BB"
    elif rsi<=state.rsi_buy+5 and sc.get("macd",0)==+2 and bb_pct<25:
        exit_sig="EXIT SHORT 🔔"; exit_rsn="RSI oversold + MACD bullish + Near lower BB"

    return dict(
        price=price,tf=state.active_tf,
        ts=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        rsi_val=rsi,rsi_sig=rsi_sig,
        k_val=k_n,d_val=d_n,stoch_sig=stoch_sig,
        ema_fast=_v(last,"ema_fast"),ema_slow=_v(last,"ema_slow"),ema_sig=ema_sig,
        macd_val=m_n,hist_val=h_n,macd_sig=macd_sig,
        bb_upper=bb_u,bb_lower=bb_l,bb_pct=bb_pct,bb_sig=bb_sig,
        adx_val=adx_v,adx_sig=adx_sig,trending=trending,
        obv_sig=obv_sig,vwap_sig=vwap_sig,vol_sig=vol_sig,
        regime_sig=regime_sig,atr_val=atr,sma200=sma200,
        patterns=patterns,
        mtf=mtf,mtf_note=mtf_note,mtf_conflict=mtf_conflict,
        fg=fg,fg_note=fg_note,fg_warn_long=fg_warn_long,
        scores=sc,raw_score=raw_score,
        adx_filtered=adx_filt,score=comp,composite=composite,
        is_long=is_long,is_short=is_short,
        stop_loss=sl,take_profit1=tp1,take_profit2=tp2,rr2=rr2,
        mode=state.mode,exit_signal=exit_sig,exit_reason=exit_rsn,
    )

async def run_analysis(symbol):
    df=await fetch_ohlcv(symbol,state.active_tf)
    if df is None: return None
    df=compute_indicators(df)
    if len(df)<3: return None
    df4h=await fetch_ohlcv(symbol,CONFIRM_TF)
    mtf=compute_mtf_bias(df4h)
    fg=await fetch_fear_greed() if USE_FEAR_GREED else None
    return generate_signals(df,mtf,fg)

# ══════════════════════════════════════════════════════════════════════
#  🎨  FORMATTING
# ══════════════════════════════════════════════════════════════════════

SIG_ICON  = {"STRONG BUY":"🟢🟢","BUY":"🟢","NEUTRAL":"⚪️","SELL":"🔴","STRONG SELL":"🔴🔴"}
MODE_ICON = {"conservative":"🛡","balanced":"⚖️","sensitive":"⚡"}

def cico(c): return SIG_ICON.get(c,"⚪️")

def signal_grade(s: dict) -> str:
    """
    Grade system: S / A / B / C / D
    Based on score, MTF confirmation, volume, and candlestick patterns.
    """
    score   = abs(s["score"])
    has_mtf = not s.get("mtf_conflict", False)
    has_vol = s.get("scores",{}).get("vol",0) > 0
    has_pat = len(s.get("patterns",[])) > 0
    has_cdl = s.get("scores",{}).get("candle",0) != 0

    bonus = sum([has_mtf, has_vol, has_pat or has_cdl])

    if score >= 10 and bonus >= 2:  return "S"   # Rare perfect signal
    if score >= 8  and bonus >= 2:  return "A"   # Very strong
    if score >= 6  and bonus >= 1:  return "B"   # Good signal
    if score >= 4:                  return "C"   # Moderate
    return "D"                                    # Weak

GRADE_LABEL = {
    "S": "🏆 Grade S — পারফেক্ট signal",
    "A": "⭐ Grade A — খুব strong",
    "B": "✅ Grade B — ভালো signal",
    "C": "⚠️ Grade C — moderate, সতর্ক থাকো",
    "D": "❌ Grade D — weak, এড়িয়ে চলো",
}

def score_bar(score: int, maxv: int = 12) -> str:
    """Visual score bar with direction."""
    filled = min(abs(score), maxv)
    empty  = maxv - filled
    bar    = "█" * filled + "░" * empty
    arrow  = "▲" if score > 0 else "▼" if score < 0 else "─"
    return f"[{bar}] {arrow}"

def ind_dot(val: int) -> str:
    """Single indicator dot: green/red/gray."""
    if val > 0:  return "🟢"
    if val < 0:  return "🔴"
    return "⚪️"

def fmt_signal(symbol: str, s: dict) -> str:
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
    coin  = s2coin(symbol)
    icon  = cico(s["composite"])
    grade = signal_grade(s)
    sc    = s["scores"]
    m_ico = MODE_ICON.get(s["mode"],"")

    sl_pct = abs(s["price"]-s["stop_loss"])    /s["price"]*100
    t1_pct = abs(s["price"]-s["take_profit1"]) /s["price"]*100
    t2_pct = abs(s["price"]-s["take_profit2"]) /s["price"]*100

    # ── Direction block ───────────────────────────────────────────────
    if s["is_long"]:
        dir_header = "📈 *LONG  —  BUY করো*"
        action_tip = "_Spot Buy → SL set করো → TP1/TP2 তে sell করো_"
        how = (
            f"*── কীভাবে trade করবে ──────────*\n"
            f"1️⃣  Gate.io / Binance খোলো\n"
            f"2️⃣  *{coin}/USDT* Spot → BUY\n"
            f"3️⃣  Stop Loss: `${s['stop_loss']:,.4f}` তে order দাও\n"
            f"4️⃣  TP1 hit হলে ৫০% বেচো: `${s['take_profit1']:,.4f}`\n"
            f"5️⃣  বাকি ৫০% TP2 পর্যন্ত রাখো: `${s['take_profit2']:,.4f}`\n"
        )
    elif s["is_short"]:
        dir_header = "📉 *SHORT  —  SELL করো*"
        action_tip = "_Hold থাকলে Sell করো → re-entry lower এ_"
        how = (
            f"*── কীভাবে trade করবে ──────────*\n"
            f"1️⃣  Gate.io / Binance খোলো\n"
            f"2️⃣  {coin} hold থাকলে SELL করো\n"
            f"3️⃣  SL নজরে রাখো: `${s['stop_loss']:,.4f}`\n"
            f"4️⃣  TP1: `${s['take_profit1']:,.4f}`\n"
            f"5️⃣  TP2: `${s['take_profit2']:,.4f}`\n"
        )
    else:
        dir_header = "➡️ *NEUTRAL  —  অপেক্ষা করো*"
        action_tip = "_Signal এখনো যথেষ্ট strong নয়_"
        how = "_পরের auto-scan এ আবার দেখবো।_\n"

    # ── Score bar ─────────────────────────────────────────────────────
    bar = score_bar(s["score"])

    # ── Indicator dots (quick visual) ─────────────────────────────────
    dots = (
        f"{ind_dot(sc.get('rsi',0))}RSI "
        f"{ind_dot(sc.get('stoch',0))}SRSI "
        f"{ind_dot(sc.get('ema',0))}EMA "
        f"{ind_dot(sc.get('macd',0))}MACD "
        f"{ind_dot(sc.get('bb',0))}BB "
        f"{ind_dot(sc.get('obv',0))}OBV "
        f"{ind_dot(sc.get('vwap',0))}VWAP "
        f"{ind_dot(sc.get('vol',0))}VOL"
    )

    # ── Warnings ──────────────────────────────────────────────────────
    warnings = []
    if s.get("mtf_conflict"):
        warnings.append("⚠️ *4H trend বিপরীত* — position size কমাও!")
    if s.get("fg_warn_long") and s["is_long"]:
        warnings.append("⚠️ *Extreme Greed* — overbought market, সতর্ক থাকো")
    if s["adx_filtered"]:
        warnings.append("⚠️ *Sideways market* (ADX weak) — score halved")
    warn_block = ("\n" + "\n".join(warnings) + "\n") if warnings else ""

    # ── Patterns ──────────────────────────────────────────────────────
    pat_line = ""
    if s["patterns"]:
        pat_line = f"🕯 *Patterns:* {' · '.join(s['patterns'])}\n"

    # ── Indicator detail (grouped) ────────────────────────────────────
    trend_block = (
        f"  {ind_dot(sc.get('ema',0))} EMA{EMA_FAST}/{EMA_SLOW}  →  {s['ema_sig']}\n"
        f"  {ind_dot(sc.get('macd',0))} MACD       →  {s['macd_sig']}\n"
        f"  {ind_dot(sc.get('regime',0))} Regime     →  {s['regime_sig']}\n"
        f"  {ind_dot(sc.get('vwap',0))} VWAP       →  {s['vwap_sig']}\n"
        f"  ◽ ADX        →  {s['adx_sig']}\n"
    )
    momentum_block = (
        f"  {ind_dot(sc.get('rsi',0))} RSI `{s['rsi_val']:.1f}`  →  {s['rsi_sig']}\n"
        f"  {ind_dot(sc.get('stoch',0))} StochRSI K:`{s['k_val']:.1f}` D:`{s['d_val']:.1f}`  →  {s['stoch_sig']}\n"
        f"  {ind_dot(sc.get('bb',0))} BB `{s['bb_pct']:.0f}%`  →  {s['bb_sig']}\n"
    )
    volume_block = (
        f"  {ind_dot(sc.get('obv',0))} OBV     →  {s['obv_sig']}\n"
        f"  {ind_dot(sc.get('vol',0))} Volume  →  {s['vol_sig']}\n"
    )

    return (
        f"{icon}{icon} *{coin}/USDT* {icon}{icon}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{dir_header}\n"
        f"{action_tip}\n\n"

        f"*📊 Signal Grade:* `{grade}`  —  {GRADE_LABEL[grade]}\n"
        f"`{bar}` `{s['score']:+d}/12`\n"
        f"{dots}\n"
        f"{pat_line}"
        f"{warn_block}\n"

        f"*💰 Entry & Levels*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"💵 Entry Price : `${s['price']:,.4f}`\n"
        f"🛑 Stop Loss   : `${s['stop_loss']:,.4f}`  _(-{sl_pct:.1f}%)_\n"
        f"🎯 TP1 (50%)   : `${s['take_profit1']:,.4f}`  _(+{t1_pct:.1f}%)_\n"
        f"🎯 TP2 (50%)   : `${s['take_profit2']:,.4f}`  _(+{t2_pct:.1f}%)_\n"
        f"📐 R:R Ratio   : `1 : {s['rr2']:.1f}`\n"
        f"⏱  Timeframe  : `{s['tf']}` + `{CONFIRM_TF}` MTF\n"
        f"🔒 Candle      : _Confirmed — no repaint_\n\n"

        f"*📈 Trend Indicators*\n"
        f"{trend_block}\n"
        f"*⚡ Momentum Indicators*\n"
        f"{momentum_block}\n"
        f"*📦 Volume Indicators*\n"
        f"{volume_block}\n"

        f"*🔍 Filters*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"4H MTF  :  {s['mtf_note']}\n"
        f"F&G     :  {s['fg_note']}\n\n"

        f"*🛒 Trade Steps*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{how}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{m_ico} `{s['mode'].upper()}` mode  ·  🕒 _{s['ts']}_\n"
        f"_⚠️ Not financial advice. নিজে research করো।_"
    )


def fmt_exit(symbol: str, s: dict) -> str:
    coin = s2coin(symbol)
    is_long_exit = "LONG" in s.get("exit_signal","")
    icon = "🔴" if is_long_exit else "🟢"
    action = "SELL করো (position close)" if is_long_exit else "BUY করো (short cover)"
    return (
        f"🔔🔔 *EXIT SIGNAL — {coin}/USDT* 🔔🔔\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"{icon} *{s['exit_signal']}*\n\n"
        f"💵 Current Price : `${s['price']:,.4f}`\n"
        f"📋 Reason: _{s['exit_reason']}_\n\n"
        f"RSI: `{s['rsi_val']:.1f}` · MACD: {s['macd_sig']}\n"
        f"BB position: `{s['bb_pct']:.0f}%` of band\n\n"
        f"*👉 এখন করো:* {action}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🕒 _{s['ts']}_"
    )


def fmt_scan_row(symbol: str, s: dict) -> str:
    coin  = s2coin(symbol)
    grade = signal_grade(s)
    f_    = "~" if s["adx_filtered"] else ""
    mtf   = "✅" if not s.get("mtf_conflict") else "⚠️"
    pat   = "🕯" if s["patterns"] else "  "
    return (
        f"{cico(s['composite'])} *{coin}*"
        f"  `${s['price']:,.4f}`"
        f"  `{f_}{s['score']:+d}/12`"
        f"  `{grade}`"
        f"  {pat}{mtf}"
    )


def fmt_price_alert(symbol: str, price: float, target: float, direction: str, sig: dict = None) -> str:
    """Rich price alert with optional signal context."""
    coin  = s2coin(symbol)
    arrow = "🚀" if direction=="above" else "📉"
    pct   = abs(price-target)/target*100

    sig_note = ""
    if sig and sig.get("composite") != "NEUTRAL":
        grade = signal_grade(sig)
        sig_note = (
            f"\n\n*📊 Current Signal:*\n"
            f"{cico(sig['composite'])} {sig['composite']}  `Grade {grade}`  `{sig['score']:+d}/12`\n"
            f"SL: `${sig['stop_loss']:,.4f}` · TP2: `${sig['take_profit2']:,.4f}`\n"
            f"_/analyze {coin} দিয়ে full signal দেখো_"
        )

    return (
        f"{arrow}{arrow} *PRICE ALERT — {coin}/USDT* {arrow}{arrow}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"Current : `${price:,.4f}`\n"
        f"Target  : `${target:,.2f}` ({direction})\n"
        f"Moved   : `{pct:.1f}%` from target\n"
        f"{sig_note}\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🕒 _{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_"
    )


def fmt_scan_summary(lines: list, signals: list, tf: str, mode: str) -> str:
    """Full scan summary with grouped BUY/SELL/NEUTRAL."""
    buys    = [(s,sig) for s,sig in signals if sig["is_long"]]
    sells   = [(s,sig) for s,sig in signals if sig["is_short"]]
    m_ico   = MODE_ICON.get(mode,"")

    header = (
        f"📡 *MARKET SCAN*\n"
        f"┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"⏱ `{tf.upper()}` · {m_ico}`{mode.upper()}` · "
        f"🕒 _{datetime.utcnow().strftime('%d %b %H:%M UTC')}_\n\n"
    )

    body = "\n".join(lines) + "\n"

    footer = f"\n🟢 *{len(buys)} BUY*  ·  🔴 *{len(sells)} SELL*"
    if not signals:
        footer += "\n_কোনো actionable signal নেই — অপেক্ষা করো।_"
    else:
        footer += f"\n_নিচের full signal দেখে manually trade নাও।_"

    return header + body + footer

# ══════════════════════════════════════════════════════════════════════
#  🤖  COMMANDS
# ══════════════════════════════════════════════════════════════════════

HELP_TEXT=f"""
📡 *Crypto Signal Bot — Manual Trading*
_Signal দেখো → Gate / Binance এ manually trade নাও_

*📊 Analysis*
`/analyze BTC` — full signal + grade + trade steps
`/scan` — সব coin scan (grouped by BUY/SELL)
`/tf 1h` — timeframe বদলাও _(1m 5m 15m 1h 4h 1d)_

*🎛 Signal Mode*
`/mode sensitive` ⚡ — বেশি signal
`/mode balanced` ⚖️ — recommended
`/mode conservative` 🛡 — কম কিন্তু accurate

*📋 Watchlist*
`/watch` · `/add SOL` · `/remove SOL`

*🔔 Price Alert*
`/alert BTC 70000 above` — price hit এ notify
`/alert ETH 2500 below`
`/alerts` — সব active alert
`/cancelalert BTC`

*⚙️ Settings*
`/threshold 25 75` — RSI levels

*📐 Grade System*
`S` 🏆 Perfect · `A` ⭐ Very strong
`B` ✅ Good · `C` ⚠️ Moderate · `D` ❌ Weak

💡 _শুধু_ `BTC` _লিখলেই signal আসবে!_
⏰ _Auto scan: {' · '.join(SCAN_TIMES)} UTC_
_Primary TF: {PRIMARY_TF} · Confirm TF: {CONFIRM_TF}_
"""

async def cmd_start(u, c):
    kb = [[
        InlineKeyboardButton("📊 BTC Signal",  callback_data="quick_BTC"),
        InlineKeyboardButton("📡 Scan All",    callback_data="scan"),
    ],[
        InlineKeyboardButton("📋 Watchlist",   callback_data="watch"),
        InlineKeyboardButton("🔔 Alerts",      callback_data="alerts"),
    ],[
        InlineKeyboardButton("⚙️ Settings",    callback_data="settings"),
        InlineKeyboardButton("❓ Help",         callback_data="help"),
    ]]
    await u.message.reply_text(
        f"📡 *Crypto Signal Bot*\n"
        f"_Signal দেখো → manually trade নাও_\n\n"
        f"Mode   : {MODE_ICON.get(state.mode,'')} `{state.mode.upper()}`\n"
        f"TF     : `{state.active_tf}` + `{CONFIRM_TF}` MTF\n"
        f"Coins  : `{len(state.watchlist)}`\n"
        f"Grades : S 🏆 A ⭐ B ✅ C ⚠️ D ❌\n\n"
        f"_BTC লিখলেই সাথে সাথে signal পাবে!_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_help(u,c): await u.message.reply_text(HELP_TEXT,parse_mode=ParseMode.MARKDOWN)

async def cmd_analyze(update,ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/analyze BTC`",parse_mode=ParseMode.MARKDOWN); return
    coin=ctx.args[0].upper().replace("USDT","").strip("/")
    if not is_valid_coin(coin): await update.message.reply_text("❌ Invalid.",parse_mode=ParseMode.MARKDOWN); return
    symbol=coin2s(coin)
    wait=await update.message.reply_text(f"⏳ *{coin}* analyze করছি...",parse_mode=ParseMode.MARKDOWN)
    sig=await run_analysis(symbol)
    if not sig: await wait.edit_text(f"❌ `{coin}` data পাওয়া গেলো না।",parse_mode=ParseMode.MARKDOWN); return
    kb=[[InlineKeyboardButton("🔄 Refresh",callback_data=f"quick_{coin}"),
         InlineKeyboardButton("🔔 Alert",  callback_data=f"alertmenu_{coin}")]]
    await wait.edit_text(fmt_signal(symbol,sig),parse_mode=ParseMode.MARKDOWN,reply_markup=InlineKeyboardMarkup(kb))
    if sig.get("exit_signal"): await update.message.reply_text(fmt_exit(symbol,sig),parse_mode=ParseMode.MARKDOWN)

async def cmd_scan(update, ctx):
    if not state.watchlist:
        await update.message.reply_text("📋 Empty! `/add BTC`", parse_mode=ParseMode.MARKDOWN)
        return
    wait = await update.message.reply_text(
        f"🔄 *{len(state.watchlist)}টা coin* scan করছি...\n_এটু সময় লাগবে_",
        parse_mode=ParseMode.MARKDOWN,
    )
    min_s   = MODES[state.mode]["min_score"]
    rows    = []
    signals = []

    for symbol in state.watchlist:
        sig = await run_analysis(symbol)
        if not sig:
            rows.append(f"⚠️ `{s2coin(symbol)}` — data error")
            continue
        rows.append(fmt_scan_row(symbol, sig))
        if abs(sig["score"]) >= min_s and sig["composite"] != "NEUTRAL":
            signals.append((symbol, sig))

    # Sort signals: STRONG first, then by score
    signals.sort(key=lambda x: abs(x[1]["score"]), reverse=True)

    summary = fmt_scan_summary(rows, signals, state.active_tf, state.mode)
    await wait.edit_text(summary, parse_mode=ParseMode.MARKDOWN)

    # Send full signal for each actionable coin
    for symbol, sig in signals:
        await update.message.reply_text(
            fmt_signal(symbol, sig), parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data=f"quick_{s2coin(symbol)}"),
                InlineKeyboardButton("🔔 Alert",   callback_data=f"alertmenu_{s2coin(symbol)}"),
            ]])
        )
        if sig.get("exit_signal"):
            await update.message.reply_text(fmt_exit(symbol, sig), parse_mode=ParseMode.MARKDOWN)

async def cmd_mode(update,ctx):
    if not ctx.args or ctx.args[0].lower() not in MODES:
        lines=["*Signal Modes:*\n"]
        for name,cfg in MODES.items():
            lines.append(f"{MODE_ICON.get(name,'')} *{name.upper()}*{' ← active' if name==state.mode else ''}\n"
                         f"  Score≥`{cfg['min_score']}/12` SL:`{cfg['atr_sl']}×ATR`")
        await update.message.reply_text("\n".join(lines)+"\n\nChange: `/mode sensitive`",parse_mode=ParseMode.MARKDOWN); return
    state.mode=ctx.args[0].lower(); state.save()
    await update.message.reply_text(f"✅ Mode → {MODE_ICON.get(state.mode,'')} *{state.mode.upper()}*",parse_mode=ParseMode.MARKDOWN)

async def cmd_tf(update,ctx):
    valid=["1m","5m","15m","1h","4h","1d"]
    if not ctx.args or ctx.args[0] not in valid:
        await update.message.reply_text(f"Usage: `/tf 1h`\nOptions: `{'` · `'.join(valid)}`",parse_mode=ParseMode.MARKDOWN); return
    state.active_tf=ctx.args[0]; state.save()
    await update.message.reply_text(f"✅ TF → `{state.active_tf}`",parse_mode=ParseMode.MARKDOWN)

async def cmd_watch(u,c):
    if not state.watchlist: await u.message.reply_text("📋 Empty.",parse_mode=ParseMode.MARKDOWN); return
    coins="  ·  ".join(f"`{s2coin(s)}`" for s in state.watchlist)
    await u.message.reply_text(f"📋 *Watchlist ({len(state.watchlist)}/{MAX_WATCHLIST})*\n\n{coins}",parse_mode=ParseMode.MARKDOWN)

async def cmd_add(update,ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/add BTC`",parse_mode=ParseMode.MARKDOWN); return
    coin=ctx.args[0].upper().replace("USDT","").strip("/")
    if not is_valid_coin(coin): await update.message.reply_text("❌ Invalid.",parse_mode=ParseMode.MARKDOWN); return
    sym=coin2s(coin)
    if sym in state.watchlist: await update.message.reply_text(f"`{coin}` already in list.",parse_mode=ParseMode.MARKDOWN); return
    if len(state.watchlist)>=MAX_WATCHLIST: await update.message.reply_text(f"❌ Full.",parse_mode=ParseMode.MARKDOWN); return
    state.watchlist.append(sym); state.save()
    await update.message.reply_text(f"✅ `{coin}` added!",parse_mode=ParseMode.MARKDOWN)

async def cmd_remove(update,ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/remove BTC`",parse_mode=ParseMode.MARKDOWN); return
    sym=coin2s(ctx.args[0].upper().replace("USDT","").strip("/"))
    if sym not in state.watchlist: await update.message.reply_text("Not found.",parse_mode=ParseMode.MARKDOWN); return
    state.watchlist.remove(sym); state.save()
    await update.message.reply_text("🗑 Removed.",parse_mode=ParseMode.MARKDOWN)

async def cmd_alert(update,ctx):
    if len(ctx.args)<3: await update.message.reply_text("Usage: `/alert BTC 70000 above`",parse_mode=ParseMode.MARKDOWN); return
    coin=ctx.args[0].upper().replace("USDT","").strip("/")
    if not is_valid_coin(coin): return
    sym=coin2s(coin)
    try: target=float(ctx.args[1].replace(",","")); assert target>0
    except: await update.message.reply_text("❌ Valid price দাও।",parse_mode=ParseMode.MARKDOWN); return
    d=ctx.args[2].lower()
    if d not in ("above","below"): await update.message.reply_text("`above` বা `below` দাও।",parse_mode=ParseMode.MARKDOWN); return
    for a in state.price_alerts[sym]:
        if a["target"]==target and a["direction"]==d: await update.message.reply_text("⚠️ Already exists.",parse_mode=ParseMode.MARKDOWN); return
    state.price_alerts[sym].append({"target":target,"direction":d}); state.save()
    await update.message.reply_text(f"🔔 {'📈' if d=='above' else '📉'} *{coin}* {d} `${target:,.2f}`",parse_mode=ParseMode.MARKDOWN)

async def cmd_alerts(update,ctx):
    active={s:al for s,al in state.price_alerts.items() if al}
    if not active: await update.message.reply_text("কোনো alert নেই।",parse_mode=ParseMode.MARKDOWN); return
    lines=["🔔 *Active Alerts*\n"]
    for sym,al_list in active.items():
        for a in al_list: lines.append(f"{'📈' if a['direction']=='above' else '📉'} *{s2coin(sym)}* {a['direction']} `${a['target']:,.2f}`")
    await update.message.reply_text("\n".join(lines),parse_mode=ParseMode.MARKDOWN)

async def cmd_cancelalert(update,ctx):
    if not ctx.args: await update.message.reply_text("Usage: `/cancelalert BTC`",parse_mode=ParseMode.MARKDOWN); return
    sym=coin2s(ctx.args[0].upper().replace("USDT","").strip("/"))
    if state.price_alerts.get(sym): state.price_alerts[sym].clear(); state.save(); await update.message.reply_text("✅ Removed.",parse_mode=ParseMode.MARKDOWN)
    else: await update.message.reply_text("কোনো alert নেই।",parse_mode=ParseMode.MARKDOWN)

async def cmd_threshold(update,ctx):
    if len(ctx.args)<2:
        await update.message.reply_text(f"RSI: BUY<`{state.rsi_buy}` · SELL>`{state.rsi_sell}`\nChange: `/threshold 25 75`",parse_mode=ParseMode.MARKDOWN); return
    try:
        b,s=int(ctx.args[0]),int(ctx.args[1]); assert 0<b<s<100
        state.rsi_buy,state.rsi_sell=b,s; state.save()
        await update.message.reply_text(f"✅ RSI: 🟢<`{b}` 🔴>`{s}`",parse_mode=ParseMode.MARKDOWN)
    except: await update.message.reply_text("❌ Example: `/threshold 25 75`",parse_mode=ParseMode.MARKDOWN)

async def handle_text(update,ctx):
    raw=update.message.text.strip().upper().replace("USDT","").strip("/")
    if is_valid_coin(raw): ctx.args=[raw]; await cmd_analyze(update,ctx)
    else: await update.message.reply_text("`BTC` বা `/help`",parse_mode=ParseMode.MARKDOWN)

async def handle_buttons(update,ctx):
    q=update.callback_query; data=q.data or ""; await q.answer()
    if data.startswith("quick_"):
        coin=data[6:].upper()
        if not is_valid_coin(coin): return
        wait=await q.message.reply_text(f"⏳ *{coin}* analyze করছি...",parse_mode=ParseMode.MARKDOWN)
        sig=await run_analysis(coin2s(coin))
        if not sig: await wait.edit_text("❌ Error.",parse_mode=ParseMode.MARKDOWN); return
        kb=[[InlineKeyboardButton("🔄 Refresh",callback_data=f"quick_{coin}"),
             InlineKeyboardButton("🔔 Alert",  callback_data=f"alertmenu_{coin}")]]
        await wait.edit_text(fmt_signal(coin2s(coin),sig),parse_mode=ParseMode.MARKDOWN,reply_markup=InlineKeyboardMarkup(kb))
        if sig.get("exit_signal"): await q.message.reply_text(fmt_exit(coin2s(coin),sig),parse_mode=ParseMode.MARKDOWN)
    elif data=="scan":   update.message=q.message; await cmd_scan(update,ctx)
    elif data=="watch":  update.message=q.message; await cmd_watch(update,ctx)
    elif data=="alerts": update.message=q.message; await cmd_alerts(update,ctx)
    elif data=="help":   await q.message.reply_text(HELP_TEXT,parse_mode=ParseMode.MARKDOWN)
    elif data=="settings":
        cfg=MODES[state.mode]
        await q.message.reply_text(
            f"⚙️ *Settings*\nMode: {MODE_ICON.get(state.mode,'')} `{state.mode.upper()}`\n"
            f"TF: `{state.active_tf}` + `{CONFIRM_TF}` MTF\n"
            f"RSI: BUY<`{state.rsi_buy}` SELL>`{state.rsi_sell}`\n"
            f"Min score: `{cfg['min_score']}/12`\n"
            f"SL:`{cfg['atr_sl']}×ATR` TP1:`{cfg['atr_tp1']}×ATR` TP2:`{cfg['atr_tp2']}×ATR`",
            parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("alertmenu_"):
        coin=data[10:].upper()
        if not is_valid_coin(coin): return
        price=await fetch_price(coin2s(coin))
        hint=f"`${price:,.4f}`" if price else "N/A"
        up=int(price*1.05) if price else "TARGET"; dn=int(price*0.95) if price else "TARGET"
        await q.message.reply_text(
            f"🔔 *{coin} Alert*\nNow: {hint}\n\n"
            f"`/alert {coin} {up} above` _(+5%)_\n`/alert {coin} {dn} below` _(-5%)_",
            parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════
#  ⏰  JOBS
# ══════════════════════════════════════════════════════════════════════

async def job_auto_scan(bot):
    if not state.watchlist: return
    log.info("Auto scan...")
    min_s   = MODES[state.mode]["min_score"]
    rows    = []
    signals = []

    for symbol in state.watchlist:
        sig = await run_analysis(symbol)
        if not sig: continue
        rows.append(fmt_scan_row(symbol, sig))
        if abs(sig["score"]) >= min_s and sig["composite"] != "NEUTRAL":
            signals.append((symbol, sig))

    signals.sort(key=lambda x: abs(x[1]["score"]), reverse=True)
    summary = fmt_scan_summary(rows, signals, state.active_tf, state.mode)
    await bot.send_message(CHAT_ID, summary, parse_mode=ParseMode.MARKDOWN)

    for symbol, sig in signals:
        await bot.send_message(
            CHAT_ID, fmt_signal(symbol, sig), parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data=f"quick_{s2coin(symbol)}"),
                InlineKeyboardButton("🔔 Alert",   callback_data=f"alertmenu_{s2coin(symbol)}"),
            ]])
        )
        if sig.get("exit_signal"):
            await bot.send_message(CHAT_ID, fmt_exit(symbol, sig), parse_mode=ParseMode.MARKDOWN)

    log.info(f"Done — {len(signals)} signal(s).")


async def job_check_alerts(bot):
    for symbol, al_list in list(state.price_alerts.items()):
        if not al_list: continue
        price = await fetch_price(symbol)
        if price is None: continue
        coin = s2coin(symbol); changed = False
        for a in list(al_list):
            hit = (
                (a["direction"]=="above" and price >= a["target"]) or
                (a["direction"]=="below" and price <= a["target"])
            )
            if hit:
                # Get current signal for context
                sig = await run_analysis(symbol)
                msg = fmt_price_alert(symbol, price, a["target"], a["direction"], sig)
                await bot.send_message(CHAT_ID, msg, parse_mode=ParseMode.MARKDOWN)
                al_list.remove(a); changed = True
        if changed: state.save()

# ══════════════════════════════════════════════════════════════════════
#  ⏰  PURE ASYNCIO SCHEDULER (no apscheduler needed)
# ══════════════════════════════════════════════════════════════════════

async def _schedule_loop(bot: Bot):
    """
    Runs forever in the background.
    Checks every minute if it's time to scan or check alerts.
    No external scheduler library needed.
    """
    last_alert_check = datetime.utcnow()
    log.info(f"⏰ Scheduler running — Scans: {SCAN_TIMES} UTC, Alerts every {ALERT_CHECK_MINS}m")

    while True:
        await asyncio.sleep(30)  # check every 30 seconds
        now = datetime.utcnow()

        # Check price alerts
        diff_mins = (now - last_alert_check).total_seconds() / 60
        if diff_mins >= ALERT_CHECK_MINS:
            last_alert_check = now
            try:
                await job_check_alerts(bot)
            except Exception as e:
                log.error(f"Alert check error: {e}")

        # Check scan schedule
        hhmm = now.strftime("%H:%M")
        if hhmm in SCAN_TIMES and now.second < 30:
            try:
                await job_auto_scan(bot)
                await asyncio.sleep(61)  # prevent double-trigger within same minute
            except Exception as e:
                log.error(f"Auto scan error: {e}")


# ══════════════════════════════════════════════════════════════════════
#  🚀  MAIN
# ══════════════════════════════════════════════════════════════════════

async def post_init(app):
    asyncio.create_task(_schedule_loop(app.bot))
    log.info(f"✅ Scheduler started — Scans: {SCAN_TIMES} UTC")

async def post_shutdown(app):
    await exchange.close()
    log.info("Stopped.")

def main():
    if BOT_TOKEN=="YOUR_BOT_TOKEN_HERE": print("\n❌  BOT_TOKEN এবং CHAT_ID বসাও!\n"); return
    if not isinstance(CHAT_ID,int) or CHAT_ID<=0: print("\n❌  CHAT_ID must be integer!\n"); return
    state.load()
    log.info(f"📡 Signal Bot — {state.mode.upper()} · TF={state.active_tf} · {len(state.watchlist)} coins")
    app=(Application.builder()
         .token(BOT_TOKEN)
         .post_init(post_init)
         .post_shutdown(post_shutdown)
         .build())
    for cmd,fn in [("start",cmd_start),("help",cmd_help),("analyze",cmd_analyze),
                   ("scan",cmd_scan),("mode",cmd_mode),("tf",cmd_tf),
                   ("watch",cmd_watch),("add",cmd_add),("remove",cmd_remove),
                   ("alert",cmd_alert),("alerts",cmd_alerts),("cancelalert",cmd_cancelalert),
                   ("threshold",cmd_threshold)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    log.info("✅ Bot live!")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()