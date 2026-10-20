"""How to get more signals: (a) same rules on more markets, (b) looser rules.
Judged on both halves of 2 years of data."""
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research as R  # noqa: E402
import research2 as R2  # noqa: E402
bot = R.bot

# typical OANDA spreads (price units)
SPREAD = {
    "XAU_USD": 0.35, "EUR_USD": 0.00012, "GBP_USD": 0.00015, "USD_JPY": 0.015, "AUD_USD": 0.00014,
    "EUR_GBP": 0.00015, "XAG_USD": 0.025, "NAS100_USD": 1.5, "SPX500_USD": 0.5, "US30_USD": 2.5,
    "DE30_EUR": 1.5, "UK100_GBP": 1.0, "BCO_USD": 0.03, "WTICO_USD": 0.03, "USD_CAD": 0.00018,
    "NZD_USD": 0.00018, "GBP_JPY": 0.03, "EUR_JPY": 0.02, "USD_CHF": 0.00016, "JP225_USD": 10.0,
}
CORE = ["XAU_USD", "EUR_USD", "GBP_USD"]
TPS = (1.5, 3, 4.5)


def m15_breakout(inst, m15, _, p):
    df = R.add_daily(m15, R.resample(m15, "1D"))
    df["atr"] = bot.atr(df)
    up = ((df["d_close"] > df["d_ema50"]) & (df["d_ema20"] > df["d_ema50"])).to_numpy()
    dn = ((df["d_close"] < df["d_ema50"]) & (df["d_ema20"] < df["d_ema50"])).to_numpy()
    hh = df["high"].rolling(p["n"]).max().shift(1).to_numpy()
    ll = df["low"].rolling(p["n"]).min().shift(1).to_numpy()
    c, a = df["close"].to_numpy(), df["atr"].to_numpy()
    uk = df.index.tz_convert("Europe/London")
    ses = ((uk.hour >= 7) & (uk.hour <= 19) & (uk.weekday < 5))
    sigs = []
    for i in range(1000, len(df) - 1):
        if not ses[i]:
            continue
        if up[i] and c[i] > hh[i]:
            sigs.append((i, 1, p["atr_sl"] * a[i], 0))
        elif dn[i] and c[i] < ll[i]:
            sigs.append((i, -1, p["atr_sl"] * a[i], 0))
    return df, sigs


VARIANTS = [
    ("Current rules (48h breakout)", R.s_h1_donchian, dict(n=48, atr_sl=2.0, rr=0, hold=72)),
    ("24h breakout", R.s_h1_donchian, dict(n=24, atr_sl=2.0, rr=0, hold=72)),
    ("12h breakout", R.s_h1_donchian, dict(n=12, atr_sl=2.0, rr=0, hold=72)),
    ("48h breakout, all hours", R.s_h1_donchian, dict(n=48, atr_sl=2.0, rr=0, hold=72, h0=0, h1=23)),
    ("24h breakout, all hours", R.s_h1_donchian, dict(n=24, atr_sl=2.0, rr=0, hold=72, h0=0, h1=23)),
    ("15m chart, 24h breakout", m15_breakout, dict(n=96, atr_sl=2.0, rr=0, hold=288)),
    ("15m chart, 12h breakout", m15_breakout, dict(n=48, atr_sl=2.0, rr=0, hold=288)),
]


def summary(trs, split):
    a = R.stats([x[1] for x in trs])
    y1 = R.stats([x[1] for x in trs if x[0] < split])
    y2 = R.stats([x[1] for x in trs if x[0] >= split])
    return a, y1, y2


def main():
    data = {}
    for inst in SPREAD:
        try:
            df = R.load(inst)
            if len(df) > 20000:
                data[inst] = df
                print(inst, len(df), df.index[0].date(), df.index[-1].date(), flush=True)
            else:
                print(inst, "too little data", len(df), flush=True)
        except Exception as e:
            print(inst, "not available:", str(e)[:120], flush=True)
    t0 = min(d.index[0] for d in data.values())
    t1 = max(d.index[-1] for d in data.values())
    split = t0 + (t1 - t0) / 2
    weeks = (t1 - t0).days / 7

    results = {}
    for name, fn, p in VARIANTS:
        for inst, m15 in data.items():
            try:
                frame, sigs = fn(inst, m15, None, p)
                results[(name, inst)] = R2.simulate_tp3(frame, sigs, SPREAD[inst], p["hold"], TPS)
            except Exception:
                traceback.print_exc()
                results[(name, inst)] = []
        print("done", name, flush=True)

    L = [f"# More signals? ({weeks:.0f} weeks of data, split at {split.date()})\n",
         "Exits as the live bot: thirds at 1.5R/3R/4.5R, SL to entry after TP1, to TP1 after TP2. R after spread.\n",
         "## Same rules, every market\n",
         "| market | trades/week | win% | PF | PF Y1 | PF Y2 | total R | worst dip R |",
         "|---|---|---|---|---|---|---|---|"]
    base = VARIANTS[0][0]
    for inst in data:
        a, y1, y2 = summary(results[(base, inst)], split)
        L.append(f"| {inst} | {a['n'] / weeks:.2f} | {a['win']:.0f} | {a['pf']:.2f} | {y1['pf']:.2f} | {y2['pf']:.2f} | "
                 f"{a['tot']:+.1f} | {a['dd']:.1f} |")

    L += ["\n## Looser rules (gold + EURUSD + GBPUSD)\n",
          "| rules | trades/week | win% | PF | PF Y1 | PF Y2 | total R | worst dip R |",
          "|---|---|---|---|---|---|---|---|"]
    for name, _, _ in VARIANTS:
        trs = sorted([x for inst in CORE if inst in data for x in results[(name, inst)]])
        a, y1, y2 = summary(trs, split)
        L.append(f"| {name} | {a['n'] / weeks:.1f} | {a['win']:.0f} | {a['pf']:.2f} | {y1['pf']:.2f} | {y2['pf']:.2f} | "
                 f"{a['tot']:+.1f} | {a['dd']:.1f} |")

    L += ["\n## Looser rules, per market (PF Y1 / PF Y2, trades/week)\n",
          "| market | " + " | ".join(v[0] for v in VARIANTS) + " |",
          "|---|" + "---|" * len(VARIANTS)]
    for inst in data:
        cells = []
        for name, _, _ in VARIANTS:
            a, y1, y2 = summary(results[(name, inst)], split)
            cells.append(f"{y1['pf']:.2f}/{y2['pf']:.2f}, {a['n'] / weeks:.1f}")
        L.append(f"| {inst} | " + " | ".join(cells) + " |")
    Path("research_results.md").write_text("\n".join(L))


if __name__ == "__main__":
    main()
