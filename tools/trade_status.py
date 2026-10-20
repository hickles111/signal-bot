"""Report price movement since each open trade was signalled (writes research_results.md)."""
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

data = bot.OandaData(os.environ["OANDA_API_TOKEN"].strip())
state = json.loads(Path("state.json").read_text())
out = []
for t in state["open"]:
    m = data.candles(t["inst"], "M15", 200)
    m = m[m.index >= pd.Timestamp(t["time"])]
    out.append(f"{t['inst']} {t['side']} entry {t['entry']:.2f} sl {t['sl']:.2f} tp1 {t['tp1']:.2f}")
    out.append(f"since signal: low {m['low'].min():.2f} at {m['low'].idxmin()}, high {m['high'].max():.2f} at {m['high'].idxmax()}, "
               f"latest close {m['close'].iloc[-1]:.2f} at {m.index[-1]}")
print("\n".join(out))
Path("research_results.md").write_text("\n".join(out) or "no open trades")
