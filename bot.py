"""
Gold & FX Signal Bot -> Discord
================================
Sends ManaraFX-style trading pings (entry, SL, TP1/TP2/TP3) for gold, EUR/USD
GBP/USD and silver, then follows each trade and posts updates as targets are hit.

Strategy (picked from a 2-year test of 60+ rule sets on real prices):
  * Only trade in the direction of the daily trend (price and 20-day EMA on the
    same side of the 50-day EMA).
  * Enter when a 1-hour candle closes beyond the high/low of the last 48 hours,
    between 07:00 and 19:59 UK time.
  * Stop loss 2x the hourly ATR. TP1/TP2/TP3 at 1.5x / 3x / 4.5x the risk.
    Close a third at each TP; stop to entry after TP1, to TP1 after TP2.

Commands:
    python bot.py check          # check once and exit (used by GitHub Actions)
    python bot.py run            # keep checking every minute (leave running on a PC)
    python bot.py backtest       # test the strategy on the last year of real prices
    python bot.py stats          # results of every signal the bot has sent so far
    python bot.py test-discord   # send a test message to check your webhook
Add --dry-run to print pings instead of sending them; add --discord to backtest
to post the results to Discord.

Not financial advice. Past results don't guarantee future results.
"""

import io
import json
import math
import os
import sys
import time
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"
UK = ZoneInfo("Europe/London")

# --------------------------------------------------------------------------- #
# Instrument facts                                                             #
# --------------------------------------------------------------------------- #

INSTRUMENTS = {
    #           symbol    base   quote  contract  pip     leverage  spread (typical, price units)
    "XAU_USD": ("XAUUSD", "XAU", "USD", 100,      0.1,    20,       0.35),
    "EUR_USD": ("EURUSD", "EUR", "USD", 100_000,  0.0001, 30,       0.00012),
    "GBP_USD": ("GBPUSD", "GBP", "USD", 100_000,  0.0001, 30,       0.00015),
    "USD_JPY": ("USDJPY", "USD", "JPY", 100_000,  0.01,   30,       0.015),
    "AUD_USD": ("AUDUSD", "AUD", "USD", 100_000,  0.0001, 20,       0.00014),
    "EUR_GBP": ("EURGBP", "EUR", "GBP", 100_000,  0.0001, 30,       0.00015),
    "XAG_USD": ("XAGUSD", "XAG", "USD", 5_000,    0.001,  10,       0.025),
}

YAHOO_SYMBOLS = {
    "XAU_USD": "GC=F",       # gold futures - a few dollars off broker spot price
    "EUR_USD": "EURUSD=X",
    "GBP_USD": "GBPUSD=X",
    "USD_JPY": "JPY=X",
    "AUD_USD": "AUDUSD=X",
    "EUR_GBP": "EURGBP=X",
    "XAG_USD": "SI=F",       # silver futures - slightly off broker spot price
}


def info(inst):
    d = INSTRUMENTS[inst]
    return dict(name=d[0], base=d[1], quote=d[2], contract=d[3], pip=d[4],
                leverage=d[5], spread=d[6])


def fmt_price(inst, p):
    if inst == "XAU_USD":
        return f"{p:,.2f}"
    if inst == "XAG_USD":
        return f"{p:.3f}"
    if inst.endswith("JPY"):
        return f"{p:.3f}"
    return f"{p:.5f}"


def fmt_distance(inst, d):
    if inst in ("XAU_USD", "XAG_USD"):
        return f"${d:.2f}"
    return f"{d / info(inst)['pip']:.1f} pips"


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "discord_webhook_url": "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE",
    "data_source": "oanda",
    "oanda_api_token": "PASTE_YOUR_OANDA_DEMO_TOKEN_HERE",
    "instruments": ["XAU_USD", "XAG_USD", "EUR_USD", "GBP_USD"],
    "account_balance_gbp": 50,
    "risk_percent": 2.0,
    "max_risk_percent": 3.0,
    "min_lot": 0.01,
    "lot_step": 0.01,
    "breakout_hours": 48,
    "stop_atr": 2.0,
    "take_profits_r": [1.5, 3.0, 4.5],
    "session_start_hour_uk": 7,
    "session_end_hour_uk": 19,
    "max_trade_hours": 72,
    "max_signals_per_day": 4,
    "max_signal_age_minutes": 30,
    "send_signals_too_big_for_account": True,
    "check_every_seconds": 60,
    "backtest_days": 365,
}


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(f"Created {CONFIG_FILE.name} - fill in your Discord webhook and data token, then run again.")
        sys.exit(1)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(CONFIG_FILE.read_text()))
    # Secrets can come from environment variables (GitHub Actions secrets)
    for env, key in (("DISCORD_WEBHOOK_URL", "discord_webhook_url"), ("OANDA_API_TOKEN", "oanda_api_token")):
        if os.environ.get(env, "").strip():
            cfg[key] = os.environ[env].strip()
    for inst in cfg["instruments"]:
        if inst not in INSTRUMENTS:
            sys.exit(f"Unknown instrument {inst}. Choose from: {', '.join(INSTRUMENTS)}")
    if len(cfg["take_profits_r"]) != 3:
        sys.exit("take_profits_r needs exactly three numbers, e.g. [1.5, 3.0, 4.5]")
    return cfg


# --------------------------------------------------------------------------- #
# Market data                                                                  #
# --------------------------------------------------------------------------- #

GRAN_SECONDS = {"M15": 900, "H1": 3600}


def _to_df(rows):
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")
    return df[~df.index.duplicated(keep="last")].sort_index()


class OandaData:
    """Free real-time data from an OANDA demo (practice) account."""

    URL = "https://api-fxpractice.oanda.com/v3/instruments/{inst}/candles"

    def __init__(self, token):
        if not token or token.startswith("PASTE"):
            sys.exit("Add your OANDA demo API token (see README), or set \"data_source\": \"yahoo\".")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _get(self, inst, params):
        r = self.s.get(self.URL.format(inst=inst), params=params, timeout=30)
        if r.status_code == 401:
            sys.exit("OANDA rejected your API token - check it.")
        r.raise_for_status()
        rows = []
        for c in r.json().get("candles", []):
            if not c.get("complete"):
                continue
            m = c["mid"]
            rows.append((c["time"], float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"])))
        return rows

    def candles(self, inst, gran, count=300):
        return _to_df(self._get(inst, {"granularity": gran, "count": min(count, 5000), "price": "M"}))

    def history(self, inst, gran, days):
        start = datetime.now(timezone.utc) - timedelta(days=days)
        rows = []
        while True:
            chunk = self._get(inst, {"granularity": gran, "price": "M", "count": 5000,
                                     "from": start.strftime("%Y-%m-%dT%H:%M:%SZ")})
            if not chunk:
                break
            rows += chunk
            if len(chunk) < 5000:
                break
            start = pd.to_datetime(chunk[-1][0], utc=True).to_pydatetime() + timedelta(seconds=1)
        return _to_df(rows)


class YahooData:
    """No-signup fallback. Forex is near real-time; gold uses futures (GC=F)."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0"

    def _get(self, inst, gran, rng):
        interval = {"M15": "15m", "H1": "60m"}[gran]
        r = self.s.get(self.URL.format(sym=YAHOO_SYMBOLS[inst]),
                       params={"interval": interval, "range": rng}, timeout=30)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        now = time.time()
        rows = []
        for i, ts in enumerate(res.get("timestamp") or []):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c) or ts + GRAN_SECONDS[gran] > now:
                continue
            rows.append((ts, o, h, l, c))
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        return df[~df.index.duplicated(keep="last")].sort_index()

    def candles(self, inst, gran, count=300):
        return self._get(inst, gran, "30d" if gran == "M15" else "200d").tail(count)

    def history(self, inst, gran, days):
        days = min(days, 59) if gran == "M15" else min(days, 700)
        return self._get(inst, gran, f"{days}d")


def make_data(cfg):
    if cfg["data_source"] == "yahoo":
        return YahooData()
    return OandaData(cfg["oanda_api_token"])


# --------------------------------------------------------------------------- #
# Indicators                                                                   #
# --------------------------------------------------------------------------- #

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = rma(d.clip(lower=0), n)
    dn = rma(-d.clip(upper=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, 1e-12))


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def adx(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    a = atr(df, n)
    plus_di = 100 * rma(plus_dm, n) / a
    minus_di = 100 * rma(minus_dm, n) / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-12)
    return rma(dx, n)


def resample(df, rule):
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def add_daily(df, d1):
    """Attach the previous *completed* day's trend values to each bar (no look-ahead)."""
    d = d1.copy()
    d["d_ema50"] = ema(d["close"], 50)
    d["d_ema20"] = ema(d["close"], 20)
    d["d_close"] = d["close"]
    d["d_n"] = range(1, len(d) + 1)
    d["avail"] = d.index + pd.Timedelta(days=1)
    left = df.reset_index()
    left = left.rename(columns={left.columns[0]: "time"}).sort_values("time")
    m = pd.merge_asof(left, d[["avail", "d_ema50", "d_ema20", "d_close", "d_n"]].sort_values("avail"),
                      left_on="time", right_on="avail", direction="backward")
    return m.set_index("time")


def prepare(h1, cfg):
    """Add everything the strategy needs to 1-hour candles."""
    h = add_daily(h1, resample(h1, "1D"))
    h["atr"] = atr(h)
    n = cfg["breakout_hours"]
    h["hh"] = h["high"].rolling(n).max().shift(1)
    h["ll"] = h["low"].rolling(n).min().shift(1)
    h["close_time"] = h.index + pd.Timedelta(hours=1)
    uk = h.index.tz_convert(UK)
    h["uk_hour"] = uk.hour
    h["wd"] = uk.weekday
    return h


# --------------------------------------------------------------------------- #
# Strategy                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class Signal:
    inst: str
    side: str            # "BUY" or "SELL"
    time: str            # close time of the trigger candle (UTC ISO)
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    reasons: list
    session: str
    # live tracking
    stop: float = 0.0              # current stop (moves to entry / TP1)
    hits: int = 0                  # how many TPs hit so far
    status: str = "open"           # open / tp3 / loss / stopped_at_entry / stopped_at_tp1 / timeout
    result_r: float = 0.0
    risk_gbp: float = 0.0
    lots: float = 0.0
    too_big: bool = False
    last_seen: str = ""            # last candle processed while tracking


def session_name(hour):
    if hour < 12:
        return "London session"
    if hour < 17:
        return "London/New York overlap"
    return "New York session"


def analyse(inst, df, i, cfg):
    """Return a Signal if the closed 1-hour candle at row i is a breakout in the daily trend direction."""
    r = df.iloc[i]
    if pd.isna(r["hh"]) or pd.isna(r["d_n"]) or r["d_n"] < 60 or pd.isna(r["atr"]):
        return None
    if r["wd"] >= 5 or not (cfg["session_start_hour_uk"] <= r["uk_hour"] <= cfg["session_end_hour_uk"]):
        return None
    up = r["d_close"] > r["d_ema50"] and r["d_ema20"] > r["d_ema50"]
    down = r["d_close"] < r["d_ema50"] and r["d_ema20"] < r["d_ema50"]
    if up and r["close"] > r["hh"]:
        side, sgn = "BUY", 1
        why = [f"Daily uptrend (above the 50-day average)",
               f"Broke the {cfg['breakout_hours']}-hour high ({fmt_price(inst, r['hh'])})"]
    elif down and r["close"] < r["ll"]:
        side, sgn = "SELL", -1
        why = [f"Daily downtrend (below the 50-day average)",
               f"Broke the {cfg['breakout_hours']}-hour low ({fmt_price(inst, r['ll'])})"]
    else:
        return None
    entry = float(r["close"])
    risk = cfg["stop_atr"] * float(r["atr"])
    t1, t2, t3 = cfg["take_profits_r"]
    why.append(f"Stop sized to volatility ({cfg['stop_atr']:g}x hourly ATR)")
    return Signal(inst=inst, side=side, time=r["close_time"].isoformat(), entry=entry,
                  sl=entry - sgn * risk, tp1=entry + sgn * t1 * risk, tp2=entry + sgn * t2 * risk,
                  tp3=entry + sgn * t3 * risk, reasons=why, session=session_name(int(r["uk_hour"])),
                  stop=entry - sgn * risk)


# --------------------------------------------------------------------------- #
# Following a trade (shared by backtest and live tracking)                     #
# --------------------------------------------------------------------------- #

def step_trade(sig, bar, cfg):
    """Advance a trade by one candle. Returns a list of events.
    If a candle touches the stop, the stop is counted first (conservative)."""
    sgn = 1 if sig.side == "BUY" else -1
    risk = abs(sig.entry - sig.sl)
    tps_r = cfg["take_profits_r"]
    levels = [sig.tp1, sig.tp2, sig.tp3]
    stop_hit = bar["low"] <= sig.stop if sgn == 1 else bar["high"] >= sig.stop
    if stop_hit:
        sig.result_r += (3 - sig.hits) / 3 * sgn * (sig.stop - sig.entry) / risk
        sig.status = ["loss", "stopped_at_entry", "stopped_at_tp1"][sig.hits]
        return [sig.status]
    events = []
    while sig.hits < 3 and (bar["high"] >= levels[sig.hits] if sgn == 1 else bar["low"] <= levels[sig.hits]):
        sig.result_r += tps_r[sig.hits] / 3
        sig.hits += 1
        events.append(f"tp{sig.hits}")
        if sig.hits == 1:
            sig.stop = sig.entry
        elif sig.hits == 2:
            sig.stop = sig.tp1
    if sig.hits == 3:
        sig.status = "tp3"
    return events


def timeout_trade(sig, price):
    sgn = 1 if sig.side == "BUY" else -1
    risk = abs(sig.entry - sig.sl)
    sig.result_r += (3 - sig.hits) / 3 * sgn * (price - sig.entry) / risk
    sig.status = "timeout"


# --------------------------------------------------------------------------- #
# Position sizing for the account                                              #
# --------------------------------------------------------------------------- #

class FX:
    """Converts currencies to GBP using the latest prices."""

    def __init__(self, data):
        self.data = data
        self.cache = {}

    def price(self, inst):
        now = time.time()
        if inst not in self.cache or now - self.cache[inst][1] > 600:
            df = self.data.candles(inst, "M15", 5)
            self.cache[inst] = (float(df["close"].iloc[-1]), now)
        return self.cache[inst][0]

    def to_gbp(self, ccy):
        """How many GBP one unit of ccy is worth."""
        if ccy == "GBP":
            return 1.0
        gbpusd = self.price("GBP_USD")
        if ccy == "USD":
            return 1 / gbpusd
        if ccy == "JPY":
            return 1 / (self.price("USD_JPY") * gbpusd)
        if ccy == "EUR":
            return self.price("EUR_USD") / gbpusd
        if ccy == "AUD":
            return self.price("AUD_USD") / gbpusd
        if ccy == "XAU":
            return self.price("XAU_USD") / gbpusd
        if ccy == "XAG":
            return self.price("XAG_USD") / gbpusd
        raise ValueError(ccy)


def size_position(sig, cfg, fx):
    i = info(sig.inst)
    bal = cfg["account_balance_gbp"]
    want_risk = bal * cfg["risk_percent"] / 100
    stop = abs(sig.entry - sig.sl)
    risk_per_lot = stop * i["contract"] * fx.to_gbp(i["quote"])
    step = cfg["lot_step"]
    lots = math.floor(want_risk / risk_per_lot / step + 1e-9) * step
    lots = round(max(lots, cfg["min_lot"]), 2)
    risk = lots * risk_per_lot
    margin = lots * i["contract"] * fx.to_gbp(i["base"]) / i["leverage"]
    cant_open = margin > bal * 0.9
    too_big = risk > bal * cfg["max_risk_percent"] / 100 or cant_open
    avg_r = sum(cfg["take_profits_r"]) / 3
    return dict(lots=lots, risk=risk, full_target=risk * avg_r, margin=margin,
                risk_pct=100 * risk / bal, too_big=too_big, cant_open=cant_open,
                can_split=lots >= 3 * step - 1e-9)


# --------------------------------------------------------------------------- #
# Discord                                                                      #
# --------------------------------------------------------------------------- #

class Discord:
    def __init__(self, url, dry_run=False):
        self.url, self.dry = url, dry_run
        if not dry_run and (not url or url.startswith("PASTE")):
            sys.exit("Add your Discord webhook URL (see README).")

    def send(self, content=None, embed=None):
        payload = {"username": "Gold & FX Signals"}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]
        if self.dry:
            print("\n----- DISCORD (dry run) -----")
            if content:
                print(content)
            if embed:
                print(embed["title"])
                if embed.get("description"):
                    print(embed["description"])
                for f in embed.get("fields", []):
                    print(f"  {f['name']}: {f['value']}")
                if embed.get("footer"):
                    print(f"  ({embed['footer']['text']})")
            print("-----------------------------")
            return
        for _ in range(3):
            r = requests.post(self.url, json=payload, timeout=15)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            r.raise_for_status()
            return


def signal_embed(sig, size, cfg):
    i = info(sig.inst)
    long = sig.side == "BUY"
    risk = abs(sig.entry - sig.sl)
    chase = sig.entry + (1 if long else -1) * 0.3 * risk
    p = lambda x: fmt_price(sig.inst, x)  # noqa: E731
    t1, t2, t3 = cfg["take_profits_r"]
    desc = (f"**Entry:** {p(sig.entry)}\n"
            f"**SL:** {p(sig.sl)}  ({fmt_distance(sig.inst, risk)})\n"
            f"**TP1:** {p(sig.tp1)}  ({fmt_distance(sig.inst, t1 * risk)})\n"
            f"**TP2:** {p(sig.tp2)}  ({fmt_distance(sig.inst, t2 * risk)})\n"
            f"**TP3:** {p(sig.tp3)}  ({fmt_distance(sig.inst, t3 * risk)})")
    fields = [
        {"name": "Size", "value": f"{size['lots']:.2f} lots", "inline": True},
        {"name": "Risk", "value": f"£{size['risk']:.2f} ({size['risk_pct']:.1f}%)", "inline": True},
        {"name": "If all TPs hit", "value": f"+£{size['full_target']:.2f}", "inline": True},
        {"name": "Plan", "value": "Close ⅓ at each TP · SL to entry at TP1 · SL to TP1 at TP2", "inline": False},
        {"name": "Why", "value": "\n".join(f"✓ {x}" for x in sig.reasons), "inline": False},
        {"name": "Don't chase", "value": f"Skip if price is already past {p(chase)}", "inline": False},
    ]
    colour = 0x2ECC71 if long else 0xE74C3C
    if size["cant_open"]:
        colour = 0xF39C12
        fields.insert(0, {"name": "⚠️ Too big for your account",
                          "value": f"The smallest size ({cfg['min_lot']} lots) needs ~£{size['margin']:.0f} margin. "
                                   "Demo/watch only.", "inline": False})
    elif size["too_big"]:
        colour = 0xF39C12
        fields.insert(0, {"name": "⚠️ Too risky for your account",
                          "value": f"Even the smallest size risks £{size['risk']:.2f} ({size['risk_pct']:.0f}%). "
                                   "Demo/watch only.", "inline": False})
    elif not size["can_split"]:
        fields.insert(3, {"name": "Small size", "value": "Can't split into thirds at this size. "
                          "Use the plan on a demo with 0.03 lots, or hold all of it and follow the SL moves.",
                          "inline": False})
    return {
        "title": f"{'🟢' if long else '🔴'} {i['name']} {sig.side} NOW",
        "description": desc,
        "color": colour,
        "fields": fields,
        "footer": {"text": f"{sig.session} · Not financial advice"},
        "timestamp": sig.time,
    }


def event_message(sig, ev, cfg):
    tag = f"**{info(sig.inst)['name']} {sig.side}**"
    p = lambda x: fmt_price(sig.inst, x)  # noqa: E731
    gbp = sig.risk_gbp * sig.result_r
    money = f" (~{'+' if gbp >= 0 else '-'}£{abs(gbp):.2f})" if sig.risk_gbp else ""
    return {
        "tp1": f"✅ {tag} TP1 hit ({p(sig.tp1)}). Close ⅓ and move your SL to entry ({p(sig.entry)}). The rest is now risk-free.",
        "tp2": f"✅✅ {tag} TP2 hit ({p(sig.tp2)}). Close another ⅓ and move your SL to TP1 ({p(sig.tp1)}).",
        "tp3": f"🎯 {tag} TP3 hit ({p(sig.tp3)}). Trade complete: {sig.result_r:+.1f}R{money}.",
        "loss": f"❌ {tag} SL hit: {sig.result_r:+.1f}R{money}. Losses are part of it, stick to the plan.",
        "stopped_at_entry": f"➖ {tag} came back to entry after TP1. Closed: {sig.result_r:+.1f}R{money}.",
        "stopped_at_tp1": f"✅ {tag} stopped at TP1 after TP2. Closed: {sig.result_r:+.1f}R{money}.",
    }[ev]


# --------------------------------------------------------------------------- #
# Backtest                                                                     #
# --------------------------------------------------------------------------- #

def run_signals_over(inst, df, cfg):
    """Walk through history: one trade at a time, entry at the next candle's open."""
    trades, busy = [], -1
    spread = info(inst)["spread"]
    hold = int(cfg["max_trade_hours"])
    for i in range(len(df) - 1):
        if i <= busy:
            continue
        sig = analyse(inst, df, i, cfg)
        if not sig:
            continue
        # enter at the next candle's open, keep the same distances
        shift = float(df.iloc[i + 1]["open"]) - sig.entry
        for k in ("entry", "sl", "tp1", "tp2", "tp3", "stop"):
            setattr(sig, k, getattr(sig, k) + shift)
        j = i + 1
        while j < len(df) and j <= i + hold:
            step_trade(sig, df.iloc[j], cfg)
            if sig.status != "open":
                break
            j += 1
        if sig.status == "open":
            if i + hold >= len(df):
                continue  # still running at the end of the data
            timeout_trade(sig, float(df.iloc[i + hold]["close"]))
            j = i + hold
        sig.result_r -= spread / abs(sig.entry - sig.sl)
        trades.append(sig)
        busy = j
    return trades


def backtest(cfg, data=None):
    data = data or make_data(cfg)
    days = cfg["backtest_days"]
    print(f"Backtesting the last ~{days} days (1-hour candles)...\n")
    all_trades = []
    for inst in cfg["instruments"]:
        h1 = data.history(inst, "H1", days + 120)   # extra for the 50-day trend warm-up
        if len(h1) < 2000:
            print(f"{info(inst)['name']}: not enough data, skipped")
            continue
        df = prepare(h1, cfg)
        start = df.index[-1] - pd.Timedelta(days=days)
        trades = [t for t in run_signals_over(inst, df, cfg) if pd.Timestamp(t.time) >= start]
        all_trades += trades
        summarise(trades, info(inst)["name"], cfg)
    print()
    all_trades.sort(key=lambda t: t.time)
    summarise(all_trades, "ALL MARKETS", cfg, detail=True)
    return all_trades


def summarise(trades, label, cfg, detail=False):
    if not trades:
        print(f"{label}: no signals")
        return
    rs = [t.result_r for t in trades]
    wins = sum(1 for r in rs if r > 0.05)
    losses = sum(1 for r in rs if r < -0.05)
    other = len(rs) - wins - losses
    total = sum(rs)
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = -sum(r for r in rs if r < 0)
    pf = gross_win / gross_loss if gross_loss else float("inf")
    peak = dd = cum = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    full = sum(1 for t in trades if t.status == "tp3")
    print(f"{label}: {len(trades)} signals ({wins}W {losses}L {other} even, {full} hit TP3)\n"
          f"  won {100 * wins / len(trades):.0f}% | {total:+.1f}R | PF {pf:.2f} | dip {dd:.1f}R")
    if detail:
        risk_gbp = cfg["account_balance_gbp"] * cfg["risk_percent"] / 100
        weeks = max(1, (pd.Timestamp(trades[-1].time) - pd.Timestamp(trades[0].time)).days / 7)
        print(f"\nAbout {len(trades) / weeks:.1f} signals a week.")
        print(f"At £{risk_gbp:.2f} risk per trade: {'+' if total >= 0 else '-'}£{abs(total * risk_gbp):.2f} total,")
        print(f"worst dip -£{dd * risk_gbp:.2f}.")
        print("\nW/L = won/lost. R = amount risked per trade.")
        print("PF (profit factor): above 1.3 is decent,")
        print("below 1.0 loses money.")


# --------------------------------------------------------------------------- #
# Live bot                                                                     #
# --------------------------------------------------------------------------- #

def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
        known = set(Signal.__dataclass_fields__)
        s["open"] = [Signal(**{k: v for k, v in x.items() if k in known}) for x in s.get("open", []) if "tp1" in x]
        s["closed"] = [Signal(**{k: v for k, v in x.items() if k in known}) for x in s.get("closed", []) if "tp1" in x]
        s.setdefault("last_bar", {})
        s.setdefault("sent_per_day", {})
        return s
    return {"open": [], "closed": [], "last_bar": {}, "last_summary": "", "sent_per_day": {}}


def save_state(s):
    out = dict(s)
    out["open"] = [asdict(x) for x in s["open"]]
    out["closed"] = [asdict(x) for x in s["closed"]]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(STATE_FILE)


def market_closed(now_utc):
    """Forex/gold: roughly Friday 22:00 UTC to Sunday 22:00 UTC."""
    wd, h = now_utc.weekday(), now_utc.hour
    return wd == 5 or (wd == 4 and h >= 22) or (wd == 6 and h < 21)


def manage_open(state, data, discord, cfg):
    """Follow open trades on 15-minute candles and post updates."""
    still_open = []
    for sig in state["open"]:
        m15 = data.candles(sig.inst, "M15", 400)
        m15 = m15.assign(close_time=m15.index + pd.Timedelta(minutes=15))
        bars = m15[m15["close_time"] > pd.Timestamp(sig.last_seen or sig.time)]
        for _, bar in bars.iterrows():
            events = step_trade(sig, bar, cfg)
            sig.last_seen = bar["close_time"].isoformat()
            for ev in events:
                discord.send(content=event_message(sig, ev, cfg))
            if sig.status != "open":
                break
        if sig.status == "open" and len(m15):
            age = m15["close_time"].iloc[-1] - pd.Timestamp(sig.time)
            if age > pd.Timedelta(hours=cfg["max_trade_hours"]):
                price = float(m15["close"].iloc[-1])
                timeout_trade(sig, price)
                discord.send(content=f"⏱️ **{info(sig.inst)['name']} {sig.side}** has been open "
                                     f"{cfg['max_trade_hours']}h. Close what's left at market "
                                     f"(~{fmt_price(sig.inst, price)}). Result {sig.result_r:+.1f}R.")
        if sig.status == "open":
            still_open.append(sig)
        else:
            state["closed"].append(sig)
    state["open"] = still_open


def weekly_summary(state, discord, cfg, force=False):
    now = datetime.now(UK)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    key = week_start.date().isoformat()
    if not force and (now.weekday() != 4 or now.hour < 21 or state.get("last_summary") == key):
        return
    trades = [t for t in state["closed"] if pd.Timestamp(t.time) >= pd.Timestamp(week_start)]
    if trades:
        wins = sum(t.result_r > 0.05 for t in trades)
        losses = sum(t.result_r < -0.05 for t in trades)
        r = sum(t.result_r for t in trades)
        gbp = sum(t.result_r * t.risk_gbp for t in trades)
        text = (f"📊 **Week summary**: {len(trades)} trades, {wins} won, {losses} lost, "
                f"{len(trades) - wins - losses} even. Total {r:+.1f}R (~{'+' if gbp >= 0 else '-'}£{abs(gbp):.2f}).")
    else:
        text = "📊 **Week summary**: no closed trades this week."
    if state["open"]:
        text += f" Still open: {', '.join(info(s.inst)['name'] + ' ' + s.side for s in state['open'])}."
    discord.send(content=text)
    state["last_summary"] = key


def one_pass(state, data, discord, fx, cfg, now=None):
    """Fetch prices once, update open trades, and send any new signals."""
    now = pd.Timestamp(datetime.now(timezone.utc) if now is None else now)
    manage_open(state, data, discord, cfg)

    today = now.tz_convert(UK).date().isoformat()
    for inst in cfg["instruments"]:
        df = prepare(data.candles(inst, "H1", 3000), cfg)
        last = state["last_bar"].get(inst)
        state["last_bar"][inst] = df.index[-1].isoformat()
        if last is None:
            rows = [len(df) - 1]
        else:
            rows = [i for i in range(max(0, len(df) - 3), len(df)) if df.index[i] > pd.Timestamp(last)]
        for i in rows:
            sig = analyse(inst, df, i, cfg)
            if not sig:
                continue
            if now - pd.Timestamp(sig.time) > pd.Timedelta(minutes=cfg["max_signal_age_minutes"]):
                print(f"{inst}: signal skipped - found too late, price has likely moved")
                continue
            if any(o.inst == inst for o in state["open"]):
                print(f"{inst}: signal skipped - a trade on it is already open")
                continue
            if state["sent_per_day"].get(today, 0) >= cfg["max_signals_per_day"]:
                print(f"{inst}: signal skipped - daily limit reached")
                continue
            size = size_position(sig, cfg, fx)
            sig.lots, sig.risk_gbp, sig.too_big = size["lots"], size["risk"], size["too_big"]
            if size["too_big"] and not cfg["send_signals_too_big_for_account"]:
                print(f"{inst}: signal skipped - too big for account")
                continue
            sig.last_seen = sig.time
            discord.send(content="@here" if not size["too_big"] else None, embed=signal_embed(sig, size, cfg))
            state["open"].append(sig)
            state["sent_per_day"][today] = state["sent_per_day"].get(today, 0) + 1
            print(f"{sig.side} {inst} sent")
            break

    state["closed"] = state["closed"][-500:]
    state["sent_per_day"] = dict(sorted(state["sent_per_day"].items())[-14:])
    weekly_summary(state, discord, cfg)


def run(cfg, dry_run=False, data=None):
    """Keep running on a PC: check every minute."""
    data = data or make_data(cfg)
    discord = Discord(cfg["discord_webhook_url"], dry_run)
    fx = FX(data)
    state = load_state()
    names = ", ".join(info(x)["name"] for x in cfg["instruments"])
    discord.send(content=f"🤖 Signal bot online, watching {names}.")
    print(f"Watching {names}. Press Ctrl+C to stop.")
    errors = 0
    while True:
        try:
            if market_closed(datetime.now(timezone.utc)):
                time.sleep(600)
                continue
            one_pass(state, data, discord, fx, cfg)
            save_state(state)
            errors = 0
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            errors += 1
            print(f"Error: {e}")
            traceback.print_exc()
            if errors == 5:
                try:
                    discord.send(content=f"⚠️ Bot is having trouble getting data: {e}")
                except Exception:
                    pass
        time.sleep(cfg["check_every_seconds"])


def check(cfg, dry_run=False, data=None, now=None):
    """Run one check and exit - used by GitHub Actions every 15 minutes."""
    if now is None and market_closed(datetime.now(timezone.utc)):
        print("Market closed - nothing to do.")
        return
    data = data or make_data(cfg)
    discord = Discord(cfg["discord_webhook_url"], dry_run)
    state = load_state()
    try:
        one_pass(state, data, discord, FX(data), cfg, now=now)
        state["errors"] = 0
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        state["errors"] = state.get("errors", 0) + 1
        if state["errors"] == 4:   # about an hour of failures
            discord.send(content=f"⚠️ Bot has failed 4 checks in a row: {e}")
    save_state(state)


def stats(cfg):
    state = load_state()
    print(f"Open trades: {len(state['open'])}")
    summarise(state["closed"], "Live results", cfg, detail=bool(state["closed"]))


def post_long(discord, title, text):
    """Send long text to Discord in code blocks under the 2000-character limit."""
    lines = [l for l in text.splitlines() if not l.startswith("Backtesting")]
    chunk = title + "\n```"
    for line in lines:
        if len(chunk) + len(line) > 1900:
            discord.send(content=chunk + "```")
            chunk = "```"
        chunk += "\n" + line
    discord.send(content=chunk + "```")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    cfg = load_config()
    dry = "--dry-run" in sys.argv
    if cmd == "run":
        try:
            run(cfg, dry_run=dry)
        except KeyboardInterrupt:
            print("\nStopped.")
    elif cmd == "check":
        check(cfg, dry_run=dry)
    elif cmd == "backtest":
        buf = io.StringIO()
        with redirect_stdout(buf):
            backtest(cfg)
        text = buf.getvalue()
        print(text)
        if "--discord" in sys.argv:
            post_long(Discord(cfg["discord_webhook_url"], dry), "📈 **Backtest on real prices**", text)
    elif cmd == "stats":
        stats(cfg)
    elif cmd == "test-discord":
        Discord(cfg["discord_webhook_url"], dry).send(content="✅ Test ping - your signal bot can reach this channel.")
        print("Sent. Check Discord.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
