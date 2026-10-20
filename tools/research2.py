"""Test the 1H trend-breakout strategy with ManaraFX-style exits:
close a third at TP1, TP2 and TP3, move stop to entry after TP1, to TP1 after TP2."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research as R  # noqa: E402
bot = R.bot


def simulate_tp3(df, sigs, spread, hold, tps, trail2=True):
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t, n = df.index, len(df)
    out, busy = [], -1
    for i, side, sd, _ in sigs:
        if i <= busy or i + 1 >= n or not sd > 0:
            continue
        e = o[i + 1]
        stop = e - side * sd
        levels = [e + side * k * sd for k in tps]
        hit, res, j = 0, 0.0, i + 1
        last = min(n - 1, i + hold)
        done = False
        for j in range(i + 1, last + 1):
            lo_hit = l[j] <= stop if side == 1 else h[j] >= stop
            if lo_hit:
                res += (3 - hit) / 3 * (side * (stop - e) / sd)
                done = True
                break
            while hit < 3 and (h[j] >= levels[hit] if side == 1 else l[j] <= levels[hit]):
                res += tps[hit] / 3
                hit += 1
                if hit == 1:
                    stop = e
                elif hit == 2 and trail2:
                    stop = levels[0]
            if hit == 3:
                done = True
                break
        if not done:
            if j < last:
                continue
            res += (3 - hit) / 3 * (side * (c[j] - e) / sd)
        res -= spread / sd
        out.append((t[i], res, side, sd))
        busy = j
    return out


def main():
    insts = list(bot.INSTRUMENTS)
    data = {i: R.load(i) for i in insts}
    split = {i: d.index[0] + (d.index[-1] - d.index[0]) / 2 for i, d in data.items()}
    variants = []
    for n in (48,):
        for sl in (2.0, 2.5):
            for tps in ((1, 2, 3), (1, 2, 4), (1.5, 3, 4.5), (0.5, 1, 2)):
                variants.append(dict(n=n, atr_sl=sl, tps=tps, rr=0, hold=72))
    lines = ["# ManaraFX-style exits (thirds at TP1/TP2/TP3)\n",
             "| params | trades | PF | PF Y1 | PF Y2 | total R | win% | dip R | per market R (XAU, EUR, GBP, JPY, AUD, EURGBP) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for p in variants:
        per = {}
        for inst in insts:
            frame, sigs = R.s_h1_donchian(inst, data[inst], None, p)
            per[inst] = simulate_tp3(frame, sigs, bot.info(inst)["spread"], p["hold"], p["tps"])
        allr = [x[1] for v in per.values() for x in v]
        a = R.stats(allr)
        y1 = R.stats([x[1] for k, v in per.items() for x in v if x[0] < split[k]])
        y2 = R.stats([x[1] for k, v in per.items() for x in v if x[0] >= split[k]])
        pm = ", ".join(f"{sum(x[1] for x in per[k]):+.0f}" for k in insts)
        lines.append(f"| n={p['n']} sl={p['atr_sl']} tps={p['tps']} | {a['n']} | {a['pf']:.2f} | {y1['pf']:.2f} | "
                     f"{y2['pf']:.2f} | {a['tot']:+.1f} | {a['win']:.0f} | {a['dd']:.1f} | {pm} |")
        # same, only gold + EUR/USD + GBP/USD
        sub = ["XAU_USD", "EUR_USD", "GBP_USD"]
        s = R.stats([x[1] for k in sub for x in per[k]])
        s1 = R.stats([x[1] for k in sub for x in per[k] if x[0] < split[k]])
        s2 = R.stats([x[1] for k in sub for x in per[k] if x[0] >= split[k]])
        fx = R.stats([x[1] for k in ("EUR_USD", "GBP_USD") for x in per[k]])
        lines.append(f"| ↳ gold+EUR+GBP only | {s['n']} | {s['pf']:.2f} | {s1['pf']:.2f} | {s2['pf']:.2f} | "
                     f"{s['tot']:+.1f} | {s['win']:.0f} | {s['dd']:.1f} | EUR+GBP only PF {fx['pf']:.2f} |")
        print(lines[-2], flush=True)
    Path("research_results.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
