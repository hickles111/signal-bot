"""
Strategy research: test many rule sets on ~2 years of real 15m data.
Each strategy is judged on BOTH halves of the data separately, so a rule set
only counts as good if it worked in year 1 AND year 2 (not just one lucky run).
Writes research_results.md.
"""
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

DAYS = int(os.environ.get("DAYS", "730"))
CACHE = Path("data")
UKTZ = "Europe/London"


# ----------------------------------------------------------------- data ---- #
def load(inst):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{inst}_M15.csv.gz"
    if f.exists():
        df = pd.read_csv(f, index_col=0, parse_dates=True)
    else:
        df = bot.OandaData(os.environ["OANDA_API_TOKEN"].strip()).history(inst, "M15", DAYS)
        df.to_csv(f, compression="gzip")
    df.index = pd.to_datetime(df.index, utc=True)
    return df[~df.index.duplicated(keep="last")].sort_index()


def resample(df, rule):
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def add_daily(df, d1):
    """Attach previous completed day's values (no look-ahead)."""
    d = d1.copy()
    d["d_ema50"] = bot.ema(d["close"], 50)
    d["d_ema20"] = bot.ema(d["close"], 20)
    d["d_atr"] = bot.atr(d)
    d["d_close"] = d["close"]
    d["avail"] = d.index + pd.Timedelta(days=1)
    m = pd.merge_asof(df.reset_index().rename(columns={"index": "time"}).sort_values("time"),
                      d[["avail", "d_ema50", "d_ema20", "d_atr", "d_close"]].sort_values("avail"),
                      left_on="time", right_on="avail", direction="backward")
    return m.set_index("time")


# ------------------------------------------------------------ simulator ---- #
def simulate(df, sigs, spread, hold, be_r):
    """sigs: list of (i, side, stop_distance, rr). Entry at next bar's open.
    One trade at a time. If a bar touches stop and target, stop counts (conservative)."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df.index
    n = len(df)
    out, busy = [], -1
    for i, side, sd, rr in sigs:
        if i <= busy or i + 1 >= n or not sd > 0:
            continue
        e = o[i + 1]
        sl, tp = e - side * sd, e + side * rr * sd
        be, res, j = False, None, i + 1
        last = min(n - 1, i + hold)
        for j in range(i + 1, last + 1):
            stop = e if be else sl
            if side == 1:
                hs, ht, hb = l[j] <= stop, h[j] >= tp, h[j] >= e + be_r * sd
            else:
                hs, ht, hb = h[j] >= stop, l[j] <= tp, l[j] <= e - be_r * sd
            if hs:
                res = 0.0 if be else -1.0
                break
            if ht:
                res = rr
                break
            if be_r and hb:
                be = True
        if res is None:
            if j < last:          # ran out of data
                continue
            res = side * (c[j] - e) / sd
        res -= spread / sd
        out.append((t[i], res, side, sd))
        busy = j
    return out


# ----------------------------------------------------------- strategies ---- #
def s_m15_pullback(inst, m15, h1, p):
    """Current bot rules (baseline)."""
    cfg = dict(bot.DEFAULT_CONFIG)
    cfg["min_quality_score"] = p["score"]
    cfg["reward_to_risk"] = p["rr"]
    cfg["session_start_uk"], cfg["session_end_uk"] = "07:00", "20:00"
    df = bot.prepare(m15, resample(m15, "1h"))
    sigs = []
    for i in range(110, len(df) - 1):
        s = bot.analyse(inst, df, i, cfg)
        if s:
            sigs.append((i, 1 if s.side == "BUY" else -1, abs(s.entry - s.sl), p["rr"]))
    return df, sigs


def s_london_breakout(inst, m15, h1, p):
    """Asian range (00:00-07:00 UK) broken by a 15m close between 07:00 and 10:45 UK."""
    df = add_daily(m15, resample(m15, "1D"))
    uk = df.index.tz_convert(UKTZ)
    df["day"] = uk.date
    df["hm"] = uk.hour * 60 + uk.minute
    df["ema200"] = bot.ema(df["close"], 200)
    sigs = []
    closes = df["close"].to_numpy()
    for day, g in df.groupby("day", sort=True):
        if pd.Timestamp(day).weekday() >= 5:
            continue
        asia = g[g["hm"] < 7 * 60]
        if len(asia) < 20:
            continue
        hi, lo = asia["high"].max(), asia["low"].min()
        rng = hi - lo
        datr = g["d_atr"].iloc[0]
        if not (np.isfinite(datr) and p["rmin"] * datr <= rng <= p["rmax"] * datr):
            continue
        win = g[(g["hm"] >= 7 * 60) & (g["hm"] < 11 * 60)]
        for ts, r in win.iterrows():
            side = 1 if r["close"] > hi else -1 if r["close"] < lo else 0
            if not side:
                continue
            if p["trend"]:
                up = r["d_close"] > r["d_ema50"]
                if (side == 1) != up:
                    break
            i = df.index.get_loc(ts)
            stop_dist = abs(closes[i] - (lo if side == 1 else hi)) if p["stop"] == "range" else rng / 2 + abs(closes[i] - (hi if side == 1 else lo))
            stop_dist = max(stop_dist, 0.15 * datr)
            sigs.append((i, side, stop_dist, p["rr"]))
            break
    return df, sigs


def h1_frame(m15):
    h = resample(m15, "1h")
    h = add_daily(h, resample(m15, "1D"))
    h["atr"] = bot.atr(h)
    h["rsi"] = bot.rsi(h["close"])
    h["rsi2"] = bot.rsi(h["close"], 2)
    h["ema20"], h["ema50"], h["ema200"] = bot.ema(h["close"], 20), bot.ema(h["close"], 50), bot.ema(h["close"], 200)
    h["adx"] = bot.adx(h)
    uk = h.index.tz_convert(UKTZ)
    h["hour"] = uk.hour
    h["wd"] = uk.weekday
    return h


def daily_trend(h):
    up = (h["d_close"] > h["d_ema50"]) & (h["d_ema20"] > h["d_ema50"])
    dn = (h["d_close"] < h["d_ema50"]) & (h["d_ema20"] < h["d_ema50"])
    return up.to_numpy(), dn.to_numpy()


def session_mask(h, p):
    return ((h["hour"] >= p.get("h0", 7)) & (h["hour"] <= p.get("h1", 19)) & (h["wd"] < 5)).to_numpy()


def s_h1_donchian(inst, m15, h1, p):
    """1H breakout of the last N hours' high/low in the direction of the daily trend."""
    h = h1_frame(m15)
    up, dn = daily_trend(h)
    hh = h["high"].rolling(p["n"]).max().shift(1).to_numpy()
    ll = h["low"].rolling(p["n"]).min().shift(1).to_numpy()
    c, a = h["close"].to_numpy(), h["atr"].to_numpy()
    ses = session_mask(h, p)
    sigs = []
    for i in range(250, len(h) - 1):
        if not ses[i]:
            continue
        if up[i] and c[i] > hh[i]:
            sigs.append((i, 1, p["atr_sl"] * a[i], p["rr"]))
        elif dn[i] and c[i] < ll[i]:
            sigs.append((i, -1, p["atr_sl"] * a[i], p["rr"]))
    return h, sigs


def s_h1_pullback(inst, m15, h1, p):
    """Daily trend + 1H RSI dips below a level then turns back up (buy the dip in an uptrend)."""
    h = h1_frame(m15)
    up, dn = daily_trend(h)
    r = h["rsi"].to_numpy()
    c, a = h["close"].to_numpy(), h["atr"].to_numpy()
    e50 = h["ema50"].to_numpy()
    ses = session_mask(h, p)
    lvl = p["lvl"]
    sigs = []
    for i in range(250, len(h) - 1):
        if not ses[i]:
            continue
        if up[i] and r[i - 1] < lvl and r[i] >= lvl and (not p["ema"] or c[i] > e50[i]):
            sigs.append((i, 1, p["atr_sl"] * a[i], p["rr"]))
        elif dn[i] and r[i - 1] > 100 - lvl and r[i] <= 100 - lvl and (not p["ema"] or c[i] < e50[i]):
            sigs.append((i, -1, p["atr_sl"] * a[i], p["rr"]))
    return h, sigs


def s_h1_rsi2(inst, m15, h1, p):
    """Short-term mean reversion in the daily trend direction (buy sharp dips)."""
    h = h1_frame(m15)
    up, dn = daily_trend(h)
    r2 = h["rsi2"].to_numpy()
    a = h["atr"].to_numpy()
    ses = session_mask(h, p)
    sigs = []
    for i in range(250, len(h) - 1):
        if not ses[i]:
            continue
        if up[i] and r2[i] < p["x"]:
            sigs.append((i, 1, p["atr_sl"] * a[i], p["rr"]))
        elif dn[i] and r2[i] > 100 - p["x"]:
            sigs.append((i, -1, p["atr_sl"] * a[i], p["rr"]))
    return h, sigs


STRATS = []
for sc in (5, 6):
    STRATS.append(("Current bot (15m pullback)", s_m15_pullback, dict(score=sc, rr=2.0, hold=48, be=1.0)))
for rr in (1.0, 1.5, 2.0):
    for stop in ("range", "mid"):
        for trend in (False, True):
            STRATS.append(("London breakout", s_london_breakout,
                           dict(rr=rr, stop=stop, trend=trend, rmin=0.1, rmax=0.6, hold=40, be=0.0)))
for n in (20, 48):
    for rr in (1.5, 2.5):
        for atr_sl in (1.5, 2.5):
            STRATS.append(("1H trend breakout", s_h1_donchian, dict(n=n, rr=rr, atr_sl=atr_sl, hold=72, be=0.0)))
for lvl in (35, 40, 45):
    for rr in (1.5, 2.0):
        for atr_sl in (1.5, 2.0):
            for ema_f in (False, True):
                STRATS.append(("1H trend pullback", s_h1_pullback,
                               dict(lvl=lvl, rr=rr, atr_sl=atr_sl, ema=ema_f, hold=72, be=0.0)))
for x in (5, 10):
    for rr, atr_sl in ((0.5, 2.0), (1.0, 1.5), (1.0, 2.0)):
        STRATS.append(("1H dip-buy (RSI2)", s_h1_rsi2, dict(x=x, rr=rr, atr_sl=atr_sl, hold=24, be=0.0)))


# ------------------------------------------------------------ reporting ---- #
def stats(rs):
    rs = np.asarray(rs, dtype=float)
    if len(rs) == 0:
        return dict(n=0, tot=0.0, pf=0.0, win=0.0, dd=0.0)
    gw, gl = rs[rs > 0].sum(), -rs[rs < 0].sum()
    cum = np.cumsum(rs)
    dd = float(np.max(np.maximum.accumulate(np.r_[0, cum]) - np.r_[0, cum]))
    return dict(n=len(rs), tot=float(rs.sum()), pf=float(gw / gl) if gl else 99.0,
                win=float((rs > 0).mean() * 100), dd=dd)


def main():
    t0 = time.time()
    insts = [i for i in bot.INSTRUMENTS]
    data = {i: load(i) for i in insts}
    split = {}
    for i, df in data.items():
        print(i, len(df), df.index[0].date(), df.index[-1].date(), flush=True)
        split[i] = df.index[0] + (df.index[-1] - df.index[0]) / 2
    rows = []
    for name, fn, p in STRATS:
        per = {}
        for inst in insts:
            m15 = data[inst]
            frame, sigs = fn(inst, m15, None, p)
            tr = simulate(frame, sigs, bot.info(inst)["spread"], p["hold"], p["be"])
            per[inst] = tr
        params = ", ".join(f"{k}={v}" for k, v in p.items() if k not in ("hold", "be"))
        rows.append((name, params, per))
        print(f"{time.time() - t0:6.0f}s {name} {params}", flush=True)

    lines = [f"# Strategy research ({DAYS} days of 15m data)\n"]
    lines.append("R = risk units after spread. Y1/Y2 = first/second half of the data. "
                 "PF = profit factor. sd = average stop distance.\n")
    summary = []
    for name, params, per in rows:
        all_tr = [x for v in per.values() for x in v]
        a = stats([x[1] for x in all_tr])
        y1 = stats([x[1] for inst, v in per.items() for x in v if x[0] < split[inst]])
        y2 = stats([x[1] for inst, v in per.items() for x in v if x[0] >= split[inst]])
        good_insts = sum(1 for v in per.values() if len(v) >= 10 and stats([x[1] for x in v])["pf"] > 1.1)
        summary.append((min(y1["pf"], y2["pf"]), name, params, a, y1, y2, good_insts, per))
    summary.sort(key=lambda s: -s[0])

    lines.append("## Ranked by the WORSE of the two halves (robustness)\n")
    lines.append("| # | strategy | params | trades | PF all | PF Y1 | PF Y2 | total R | win% | max dip R | markets PF>1.1 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for k, (mn, name, params, a, y1, y2, gi, per) in enumerate(summary, 1):
        lines.append(f"| {k} | {name} | {params} | {a['n']} | {a['pf']:.2f} | {y1['pf']:.2f} | {y2['pf']:.2f} | "
                     f"{a['tot']:+.1f} | {a['win']:.0f} | {a['dd']:.1f} | {gi}/{len(per)} |")

    lines.append("\n## Per-market detail for the top 8\n")
    for mn, name, params, a, y1, y2, gi, per in summary[:8]:
        lines.append(f"### {name} — {params}\n")
        lines.append("| market | trades | PF | total R | win% | longs R | shorts R | Y1 R | Y2 R | avg stop |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for inst, v in per.items():
            s = stats([x[1] for x in v])
            lr = sum(x[1] for x in v if x[2] == 1)
            sr = sum(x[1] for x in v if x[2] == -1)
            r1 = sum(x[1] for x in v if x[0] < split[inst])
            r2 = sum(x[1] for x in v if x[0] >= split[inst])
            sd = np.mean([x[3] for x in v]) if v else 0
            lines.append(f"| {inst} | {s['n']} | {s['pf']:.2f} | {s['tot']:+.1f} | {s['win']:.0f} | {lr:+.1f} | {sr:+.1f} | "
                         f"{r1:+.1f} | {r2:+.1f} | {bot.fmt_distance(inst, sd)} |")
        lines.append("")
    Path("research_results.md").write_text("\n".join(lines))
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
