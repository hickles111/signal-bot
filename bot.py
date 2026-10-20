"""
Gold & FX Signal Bot -> Discord
================================
Watches gold (XAU/USD) and forex majors on the 15-minute chart, and only pings
Discord when a high-quality trend-pullback setup lines up on two timeframes.

Commands:
    python bot.py run            # watch the market and send pings (leave running on a PC)
    python bot.py check          # check once and exit (used by GitHub Actions)
    python bot.py run --dry-run  # same, but print pings instead of sending them
    python bot.py backtest       # test the strategy on the last ~90 days of real data
    python bot.py stats          # results of every signal the bot has sent so far
    python bot.py test-discord   # send a test message to check your webhook

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
    #           display    base   quote  contract  pip     leverage  spread (typical, price units)
    "XAU_USD": ("Gold",    "XAU", "USD", 100,      0.1,    20,       0.35),
    "EUR_USD": ("EUR/USD", "EUR", "USD", 100_000,  0.0001, 30,       0.00012),
    "GBP_USD": ("GBP/USD", "GBP", "USD", 100_000,  0.0001, 30,       0.00015),
    "USD_JPY": ("USD/JPY", "USD", "JPY", 100_000,  0.01,   30,       0.015),
    "AUD_USD": ("AUD/USD", "AUD", "USD", 100_000,  0.0001, 20,       0.00014),
    "EUR_GBP": ("EUR/GBP", "EUR", "GBP", 100_000,  0.0001, 30,       0.00015),
}

YAHOO_SYMBOLS = {
    "XAU_USD": "GC=F",       # gold futures - a few dollars off broker spot price
    "EUR_USD": "EURUSD=X",
    "GBP_USD": "GBPUSD=X",
    "USD_JPY": "JPY=X",
    "AUD_USD": "AUDUSD=X",
    "EUR_GBP": "EURGBP=X",
}


def info(inst):
    d = INSTRUMENTS[inst]
    return dict(name=d[0], base=d[1], quote=d[2], contract=d[3], pip=d[4],
                leverage=d[5], spread=d[6])


def fmt_price(inst, p):
    if inst == "XAU_USD":
        return f"{p:,.2f}"
    if inst.endswith("JPY"):
        return f"{p:.3f}"
    return f"{p:.5f}"


def fmt_distance(inst, d):
    if inst == "XAU_USD":
        return f"${d:.2f}"
    return f"{d / info(inst)['pip']:.1f} pips"


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "discord_webhook_url": "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE",
    "data_source": "oanda",
    "oanda_api_token": "PASTE_YOUR_OANDA_DEMO_TOKEN_HERE",
    "instruments": ["XAU_USD", "EUR_USD", "GBP_USD", "USD_JPY"],
    "account_balance_gbp": 50,
    "risk_percent": 2.0,
    "max_risk_percent": 3.0,
    "min_lot": 0.01,
    "lot_step": 0.01,
    "min_quality_score": 5,
    "reward_to_risk": 2.0,
    "max_signals_per_day": 2,
    "cooldown_hours_per_instrument": 4,
    "session_start_uk": "07:00",
    "session_end_uk": "20:00",
    "move_to_breakeven_at_r": 1.0,
    "max_trade_hours": 12,
    "send_signals_too_big_for_account": True,
    "check_every_seconds": 60,
    "max_signal_age_minutes": 20,
    "backtest_days": 90,
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
    return cfg


# --------------------------------------------------------------------------- #
# Market data                                                                  #
# --------------------------------------------------------------------------- #

GRAN_SECONDS = {"M15": 900, "H1": 3600}


class OandaData:
    """Free real-time data from an OANDA demo (practice) account."""

    URL = "https://api-fxpractice.oanda.com/v3/instruments/{inst}/candles"

    def __init__(self, token):
        if not token or token.startswith("PASTE"):
            sys.exit("Add your OANDA demo API token to config.json (see README), "
                     "or set \"data_source\": \"yahoo\".")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _get(self, inst, params):
        r = self.s.get(self.URL.format(inst=inst), params=params, timeout=20)
        if r.status_code == 401:
            sys.exit("OANDA rejected your API token - check it in config.json.")
        r.raise_for_status()
        rows = []
        for c in r.json().get("candles", []):
            if not c.get("complete"):
                continue
            m = c["mid"]
            rows.append((c["time"], float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"])))
        return rows

    def candles(self, inst, gran, count=300):
        return _to_df(self._get(inst, {"granularity": gran, "count": count, "price": "M"}))

    def history(self, inst, gran, days):
        start = datetime.now(timezone.utc) - timedelta(days=days)
        rows = []
        while True:
            chunk = self._get(inst, {"granularity": gran, "price": "M", "count": 5000,
                                     "from": start.strftime("%Y-%m-%dT%H:%M:%SZ")})
            if not chunk:
                break
            rows += chunk
            last = pd.to_datetime(chunk[-1][0], utc=True)
            if len(chunk) < 5000:
                break
            start = last.to_pydatetime() + timedelta(seconds=1)
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
                       params={"interval": interval, "range": rng}, timeout=20)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        now = time.time()
        rows = []
        for i, ts in enumerate(res.get("timestamp") or []):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            if ts + GRAN_SECONDS[gran] > now:      # bar still forming
                continue
            rows.append((ts, o, h, l, c))
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df.set_index("time")

    def candles(self, inst, gran, count=300):
        return self._get(inst, gran, "30d" if gran == "M15" else "90d").tail(count)

    def history(self, inst, gran, days):
        days = min(days, 59) if gran == "M15" else min(days, 700)
        return self._get(inst, gran, f"{days}d")


def _to_df(rows):
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


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


def prepare(m15, h1):
    """Add indicators to both timeframes and attach the latest *closed* 1H values to each 15m bar."""
    m = m15.copy()
    m["ema20"], m["ema50"] = ema(m["close"], 20), ema(m["close"], 50)
    m["rsi"], m["atr"], m["adx"] = rsi(m["close"]), atr(m), adx(m)
    m["atr_avg"] = m["atr"].rolling(100).mean()
    m["close_time"] = m.index + pd.Timedelta(minutes=15)

    h = h1.copy()
    h["h_ema50"], h["h_ema200"] = ema(h["close"], 50), ema(h["close"], 200)
    h["h_rsi"] = rsi(h["close"])
    h["h_close"] = h["close"]
    h["close_time"] = h.index + pd.Timedelta(hours=1)
    h = h.iloc[200:]  # need warm-up for EMA200

    merged = pd.merge_asof(
        m.reset_index().sort_values("close_time"),
        h[["close_time", "h_close", "h_ema50", "h_ema200", "h_rsi"]].sort_values("close_time"),
        on="close_time", direction="backward")
    return merged.set_index("time")


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
    tp: float
    score: int
    max_score: int
    reasons: list
    session: str
    # live tracking
    status: str = "open"           # open / win / loss / breakeven / timeout
    be_moved: bool = False
    result_r: float = 0.0
    risk_gbp: float = 0.0
    lots: float = 0.0
    too_big: bool = False
    closed_time: str = ""


def in_session(ts_utc, cfg):
    t = ts_utc.astimezone(UK)
    start = datetime.strptime(cfg["session_start_uk"], "%H:%M").time()
    end = datetime.strptime(cfg["session_end_uk"], "%H:%M").time()
    if t.weekday() >= 5:
        return None
    if not (start <= t.time() <= end):
        return None
    if t.hour < 12:
        return "London session"
    if t.hour < 17:
        return "London/New York overlap"
    return "New York session"


def analyse(inst, df, i, cfg):
    """Look at the closed candle at row i. Return a Signal only if every hard rule passes
    and the quality score reaches min_quality_score."""
    if i < 110:
        return None
    r = df.iloc[i]
    if pd.isna(r.get("h_ema200")) or pd.isna(r["atr_avg"]):
        return None

    session = in_session(r["close_time"].to_pydatetime(), cfg)
    if not session:
        return None

    a = r["atr"]
    # Hard rule: normal volatility (skip dead markets and news spikes)
    if not (0.6 * r["atr_avg"] <= a <= 2.5 * r["atr_avg"]):
        return None

    prev = df.iloc[i - 1]
    recent = df.iloc[i - 4:i + 1]
    rr = cfg["reward_to_risk"]

    for side in ("BUY", "SELL"):
        long = side == "BUY"
        sgn = 1 if long else -1

        # Hard rule 1: 1-hour trend agrees
        if long and not (r["h_close"] > r["h_ema200"] and r["h_ema50"] > r["h_ema200"]):
            continue
        if not long and not (r["h_close"] < r["h_ema200"] and r["h_ema50"] < r["h_ema200"]):
            continue
        # Hard rule 2: 15-minute trend agrees
        if long and not (r["ema20"] > r["ema50"] and r["close"] > r["ema50"]):
            continue
        if not long and not (r["ema20"] < r["ema50"] and r["close"] < r["ema50"]):
            continue
        # Hard rule 3: price pulled back to the 20 EMA in the last hour
        if long and not (recent["low"] <= recent["ema20"] + 0.1 * a).any():
            continue
        if not long and not (recent["high"] >= recent["ema20"] - 0.1 * a).any():
            continue
        # Hard rule 4: bounce confirmed - candle closes in trend direction beyond prior candle
        if long and not (r["close"] > r["open"] and r["close"] > r["ema20"] and r["close"] > prev["high"]):
            continue
        if not long and not (r["close"] < r["open"] and r["close"] < r["ema20"] and r["close"] < prev["low"]):
            continue

        # Stop beyond the pullback swing, take profit at reward_to_risk x risk
        entry = r["close"]
        swing = df.iloc[i - 5:i + 1]["low"].min() if long else df.iloc[i - 5:i + 1]["high"].max()
        sl = swing - sgn * 0.2 * a
        risk = abs(entry - sl)
        if risk < 1.0 * a:
            risk = 1.0 * a
            sl = entry - sgn * risk
        if risk > 2.5 * a:            # Hard rule 5: stop would be too wide
            continue
        tp = entry + sgn * rr * risk

        # Quality points
        reasons, score = [], 0
        reasons.append(f"1H {'uptrend' if long else 'downtrend'} confirmed")
        reasons.append("Pulled back to the 20 EMA and bounced")

        checks = []
        checks.append((r["adx"] >= 20, f"Trend strength ADX {r['adx']:.0f}"))
        if long:
            checks.append((50 <= r["rsi"] <= 68, f"Momentum healthy (RSI {r['rsi']:.0f})"))
            checks.append((r["h_rsi"] > 50, f"1H momentum agrees (RSI {r['h_rsi']:.0f})"))
            checks.append((r["close"] - r["ema50"] <= 1.5 * a, "Not overstretched"))
        else:
            checks.append((32 <= r["rsi"] <= 50, f"Momentum healthy (RSI {r['rsi']:.0f})"))
            checks.append((r["h_rsi"] < 50, f"1H momentum agrees (RSI {r['h_rsi']:.0f})"))
            checks.append((r["ema50"] - r["close"] <= 1.5 * a, "Not overstretched"))
        rng = r["high"] - r["low"]
        checks.append((rng > 0 and abs(r["close"] - r["open"]) >= 0.5 * rng, "Strong confirmation candle"))
        day = df.iloc[max(0, i - 96):i]
        if long:
            blocker = day["high"].max()
            clear = blocker <= entry or blocker >= tp - 0.25 * risk
        else:
            blocker = day["low"].min()
            clear = blocker >= entry or blocker <= tp + 0.25 * risk
        checks.append((clear, "Clear path to target (no recent high/low in the way)"))

        for ok, text in checks:
            if ok:
                score += 1
                reasons.append(text)
        if score < cfg["min_quality_score"]:
            continue

        return Signal(inst=inst, side=side, time=r["close_time"].isoformat(), entry=float(entry),
                      sl=float(sl), tp=float(tp), score=score, max_score=len(checks),
                      reasons=reasons, session=session)
    return None


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
        raise ValueError(ccy)


def size_position(sig, cfg, fx):
    i = info(sig.inst)
    bal = cfg["account_balance_gbp"]
    want_risk = bal * cfg["risk_percent"] / 100
    stop = abs(sig.entry - sig.sl)
    risk_per_lot = stop * i["contract"] * fx.to_gbp(i["quote"])
    step = cfg["lot_step"]
    lots = math.floor(want_risk / risk_per_lot / step) * step
    lots = max(lots, cfg["min_lot"])
    lots = round(lots, 2)
    risk = lots * risk_per_lot
    margin = lots * i["contract"] * fx.to_gbp(i["base"]) / i["leverage"]
    too_big = risk > bal * cfg["max_risk_percent"] / 100
    cant_open = margin > bal * 0.9
    return dict(lots=lots, risk=risk, reward=risk * cfg["reward_to_risk"], margin=margin,
                risk_pct=100 * risk / bal, too_big=too_big or cant_open, cant_open=cant_open)


# --------------------------------------------------------------------------- #
# Discord                                                                      #
# --------------------------------------------------------------------------- #

class Discord:
    def __init__(self, url, dry_run=False):
        self.url, self.dry = url, dry_run
        if not dry_run and (not url or url.startswith("PASTE")):
            sys.exit("Add your Discord webhook URL to config.json (see README).")

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
    colour = 0x2ECC71 if long else 0xE74C3C
    fields = [
        {"name": "Entry (now)", "value": fmt_price(sig.inst, sig.entry), "inline": True},
        {"name": "Stop loss", "value": f"{fmt_price(sig.inst, sig.sl)}\n({fmt_distance(sig.inst, risk)})", "inline": True},
        {"name": "Take profit", "value": f"{fmt_price(sig.inst, sig.tp)}\n({fmt_distance(sig.inst, risk * cfg['reward_to_risk'])})", "inline": True},
        {"name": "Size", "value": f"{size['lots']:.2f} lots", "inline": True},
        {"name": "Risk / target", "value": f"£{size['risk']:.2f} ({size['risk_pct']:.1f}%) / £{size['reward']:.2f}", "inline": True},
        {"name": "Margin needed", "value": f"~£{size['margin']:.0f}", "inline": True},
        {"name": "Why", "value": "\n".join(f"✓ {x}" for x in sig.reasons), "inline": False},
        {"name": "Don't chase", "value": f"Skip it if price is already past {fmt_price(sig.inst, chase)}", "inline": False},
    ]
    if size["cant_open"]:
        colour = 0xF39C12
        fields.insert(0, {"name": "⚠️ Too big for your account",
                          "value": f"The smallest size ({cfg['min_lot']} lots) needs ~£{size['margin']:.0f} margin. "
                                   f"Demo/watch only.", "inline": False})
    elif size["too_big"]:
        colour = 0xF39C12
        fields.insert(0, {"name": "⚠️ Too risky for your account",
                          "value": f"Even the smallest size risks £{size['risk']:.2f} ({size['risk_pct']:.0f}%). "
                                   f"Demo/watch only.", "inline": False})
    return {
        "title": f"{'🟢 BUY' if long else '🔴 SELL'} {i['name']}",
        "description": f"Quality **{sig.score}/{sig.max_score}** · {sig.session}",
        "color": colour,
        "fields": fields,
        "footer": {"text": f"Move stop to entry at +{cfg['move_to_breakeven_at_r']:g}R · Not financial advice"},
        "timestamp": sig.time,
    }


# --------------------------------------------------------------------------- #
# Trade outcome simulation (used by backtest and live tracking)                #
# --------------------------------------------------------------------------- #

def step_trade(sig, bar, cfg):
    """Advance a trade by one candle. Returns an event string or None.
    Conservative: if a candle touches both stop and target, count the stop."""
    long = sig.side == "BUY"
    risk = abs(sig.entry - sig.sl)
    stop = sig.entry if sig.be_moved else sig.sl
    hit_stop = bar["low"] <= stop if long else bar["high"] >= stop
    hit_tp = bar["high"] >= sig.tp if long else bar["low"] <= sig.tp
    be_level = sig.entry + (1 if long else -1) * cfg["move_to_breakeven_at_r"] * risk
    hit_be = bar["high"] >= be_level if long else bar["low"] <= be_level

    if hit_stop:
        if sig.be_moved:
            sig.status, sig.result_r = "breakeven", 0.0
        else:
            sig.status, sig.result_r = "loss", -1.0
        return sig.status
    if hit_tp:
        sig.status, sig.result_r = "win", cfg["reward_to_risk"]
        return "win"
    if hit_be and not sig.be_moved and cfg["move_to_breakeven_at_r"] > 0:
        sig.be_moved = True
        return "breakeven_move"
    return None


def timeout_trade(sig, price):
    long = sig.side == "BUY"
    risk = abs(sig.entry - sig.sl)
    sig.result_r = ((price - sig.entry) if long else (sig.entry - price)) / risk
    sig.status = "timeout"


# --------------------------------------------------------------------------- #
# Backtest                                                                     #
# --------------------------------------------------------------------------- #

def backtest(cfg, data=None, quiet=False):
    data = data or make_data(cfg)
    days = cfg["backtest_days"]
    all_trades = []
    print(f"Backtesting the last ~{days} days (15-minute candles). This can take a minute...\n")
    for inst in cfg["instruments"]:
        m15 = data.history(inst, "M15", days)
        h1 = data.history(inst, "H1", days + 20)
        if len(m15) < 300 or len(h1) < 260:
            print(f"{inst}: not enough data, skipped")
            continue
        df = prepare(m15, h1)
        trades = run_signals_over(inst, df, cfg)
        all_trades += trades
        if not quiet:
            summarise(trades, f"{info(inst)['name']}", cfg)
    print()
    summarise(all_trades, "ALL MARKETS", cfg, detail=True)
    return all_trades


def run_signals_over(inst, df, cfg):
    trades, open_until = [], -1
    spread = info(inst)["spread"]
    per_day = {}
    last_sig_time = None
    max_bars = int(cfg["max_trade_hours"] * 4)
    for i in range(len(df) - 1):
        if i <= open_until:
            continue
        sig = analyse(inst, df, i, cfg)
        if not sig:
            continue
        t = df.iloc[i]["close_time"]
        day = t.astimezone(UK).date()
        if per_day.get(day, 0) >= cfg["max_signals_per_day"]:
            continue
        if last_sig_time is not None and t - last_sig_time < pd.Timedelta(hours=cfg["cooldown_hours_per_instrument"]):
            continue
        per_day[day] = per_day.get(day, 0) + 1
        last_sig_time = t
        j = i + 1
        while j < len(df) and j <= i + max_bars:
            if step_trade(sig, df.iloc[j], cfg) in ("win", "loss", "breakeven"):
                break
            j += 1
        if sig.status == "open":
            if j > i + max_bars and i + max_bars < len(df):
                timeout_trade(sig, df.iloc[i + max_bars]["close"])
            else:
                continue  # still running at end of data - ignore
        sig.result_r -= spread / abs(sig.entry - sig.sl)   # pay the spread
        sig.closed_time = df.index[min(j, len(df) - 1)].isoformat()
        trades.append(sig)
        open_until = j
    return trades


def summarise(trades, label, cfg, detail=False):
    if not trades:
        print(f"{label:<14} no signals")
        return
    rs = [t.result_r for t in trades]
    wins = sum(1 for t in trades if t.status == "win")
    losses = sum(1 for t in trades if t.status == "loss")
    other = len(trades) - wins - losses
    total = sum(rs)
    gross_win = sum(r for r in rs if r > 0)
    gross_loss = -sum(r for r in rs if r < 0)
    pf = gross_win / gross_loss if gross_loss else float("inf")
    peak = dd = cum = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    risk_gbp = cfg["account_balance_gbp"] * cfg["risk_percent"] / 100
    print(f"{label}: {len(trades)} signals ({wins}W {losses}L {other} even)\n"
          f"  won {100 * wins / len(trades):.0f}% | {total:+.1f}R | PF {pf:.2f} | dip {dd:.1f}R")
    if detail:
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
        s["open"] = [Signal(**x) for x in s.get("open", [])]
        s["closed"] = [Signal(**x) for x in s.get("closed", [])]
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


def manage_open(state, frames, discord, cfg):
    still_open = []
    for sig in state["open"]:
        df = frames.get(sig.inst)
        if df is None:
            still_open.append(sig)
            continue
        name = info(sig.inst)["name"]
        last_seen = pd.Timestamp(sig.closed_time or sig.time)
        bars = df[df["close_time"] > last_seen]
        done = False
        for _, bar in bars.iterrows():
            ev = step_trade(sig, bar, cfg)
            sig.closed_time = bar["close_time"].isoformat()
            if ev == "breakeven_move":
                discord.send(content=f"🛡️ **{name} {sig.side}** is up {cfg['move_to_breakeven_at_r']:g}R - "
                                     f"move your stop loss to entry ({fmt_price(sig.inst, sig.entry)}) so it can't lose.")
            elif ev in ("win", "loss", "breakeven"):
                msg = {"win": f"✅ **{name} {sig.side}** hit take profit: +{sig.result_r:g}R (~+£{sig.risk_gbp * sig.result_r:.2f})",
                       "loss": f"❌ **{name} {sig.side}** hit stop loss: -1R (~-£{sig.risk_gbp:.2f}). Part of the game - stick to the plan.",
                       "breakeven": f"➖ **{name} {sig.side}** came back to entry after the stop was moved: 0R, no loss."}[ev]
                discord.send(content=msg)
                done = True
                break
        if not done and sig.status == "open":
            age = df["close_time"].iloc[-1] - pd.Timestamp(sig.time)
            if age > pd.Timedelta(hours=cfg["max_trade_hours"]):
                price = float(df["close"].iloc[-1])
                timeout_trade(sig, price)
                discord.send(content=f"⏱️ **{name} {sig.side}** has been open {cfg['max_trade_hours']}h - close it now at market "
                                     f"(~{fmt_price(sig.inst, price)}, {sig.result_r:+.1f}R).")
                done = True
        if done:
            state["closed"].append(sig)
        else:
            still_open.append(sig)
    state["open"] = still_open


def weekly_summary(state, discord, cfg, force=False):
    now = datetime.now(UK)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    key = week_start.date().isoformat()
    if not force and (now.weekday() != 4 or now.hour < 21 or state.get("last_summary") == key):
        return
    trades = [t for t in state["closed"] if pd.Timestamp(t.time) >= pd.Timestamp(week_start)]
    if trades:
        wins = sum(t.status == "win" for t in trades)
        losses = sum(t.status == "loss" for t in trades)
        r = sum(t.result_r for t in trades)
        gbp = sum(t.result_r * t.risk_gbp for t in trades)
        text = (f"📊 **Week summary** - {len(trades)} signals: {wins} won, {losses} lost, "
                f"{len(trades) - wins - losses} breakeven/timed out. Total {r:+.1f}R (~{'+' if gbp >= 0 else '-'}£{abs(gbp):.2f}).")
    else:
        text = "📊 **Week summary** - no signals this week. No great setups = no trades, which is the point."
    discord.send(content=text)
    state["last_summary"] = key


def one_pass(state, data, discord, fx, cfg, now=None):
    """Fetch prices once, update open trades, and send any new signals."""
    now = pd.Timestamp(datetime.now(timezone.utc) if now is None else now)
    frames = {}
    for inst in cfg["instruments"]:
        m15 = data.candles(inst, "M15", 400)
        h1 = data.candles(inst, "H1", 400)
        frames[inst] = prepare(m15, h1)

    manage_open(state, frames, discord, cfg)

    today = now.tz_convert(UK).date().isoformat()
    for inst, df in frames.items():
        last = state["last_bar"].get(inst)
        state["last_bar"][inst] = df.index[-1].isoformat()
        # Check every candle closed since the last check (catches up if a run was late or skipped)
        if last is None:
            rows = [len(df) - 1]
        else:
            rows = [i for i in range(max(0, len(df) - 6), len(df)) if df.index[i] > pd.Timestamp(last)]
        for i in rows:
            sig = analyse(inst, df, i, cfg)
            if not sig:
                continue
            if now - pd.Timestamp(sig.time) > pd.Timedelta(minutes=cfg["max_signal_age_minutes"]):
                print(f"{inst}: signal skipped - found too late, price has likely moved")
                continue
            if state["sent_per_day"].get(today, 0) >= cfg["max_signals_per_day"]:
                print(f"{inst}: signal skipped - daily limit reached")
                continue
            if any(o.inst == inst for o in state["open"]):
                continue
            recent = [c for c in state["closed"] if c.inst == inst]
            if recent and pd.Timestamp(sig.time) - pd.Timestamp(recent[-1].time) < pd.Timedelta(hours=cfg["cooldown_hours_per_instrument"]):
                continue
            size = size_position(sig, cfg, fx)
            sig.lots, sig.risk_gbp, sig.too_big = size["lots"], size["risk"], size["too_big"]
            if size["too_big"] and not cfg["send_signals_too_big_for_account"]:
                print(f"{inst}: signal skipped - too big for account")
                continue
            discord.send(content="@here" if not size["too_big"] else None, embed=signal_embed(sig, size, cfg))
            state["open"].append(sig)
            state["sent_per_day"][today] = state["sent_per_day"].get(today, 0) + 1
            print(f"{sig.side} {inst} sent (quality {sig.score}/{sig.max_score})")
            break

    # keep the state file small
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
    discord.send(content=f"🤖 Signal bot online - watching {names} on the 15-minute chart. "
                         f"Only pings for quality {cfg['min_quality_score']}+ setups, max {cfg['max_signals_per_day']} a day.")
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
    print(f"Open signals: {len(state['open'])}")
    summarise(state["closed"], "Live results", cfg, detail=bool(state["closed"]))


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


if __name__ == "__main__":
    main()
