"""Run the bot's own backtest (2 years) and save the output as research_results.md."""
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

cfg = dict(bot.DEFAULT_CONFIG)
cfg["oanda_api_token"] = os.environ["OANDA_API_TOKEN"].strip()
cfg["backtest_days"] = int(os.environ.get("DAYS", "730"))
out = []
for label, insts in (("Gold + silver + EUR/USD + GBP/USD", ["XAU_USD", "XAG_USD", "EUR_USD", "GBP_USD"]),
                     ("Other pairs (not used, for comparison)", ["USD_JPY", "AUD_USD", "EUR_GBP"])):
    cfg["instruments"] = insts
    buf = io.StringIO()
    with redirect_stdout(buf):
        trades = bot.backtest(cfg)
    split = sorted(t.time for t in trades)[len(trades) // 2] if trades else None
    y1 = sum(t.result_r for t in trades if t.time < split) if trades else 0
    y2 = sum(t.result_r for t in trades if t.time >= split) if trades else 0
    stops = {}
    for t in trades:
        stops.setdefault(t.inst, []).append(abs(t.entry - t.sl))
    avg = ", ".join(f"{bot.info(k)['name']} {bot.fmt_distance(k, sum(v) / len(v))}" for k, v in stops.items())
    out.append(f"## {label}\n```\n{buf.getvalue()}\nFirst half {y1:+.1f}R, second half {y2:+.1f}R\nAvg stop: {avg}\n```\n")
    print(out[-1], flush=True)
Path("research_results.md").write_text("\n".join(out))
