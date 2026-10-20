"""Check that the OANDA token and Discord webhook secrets work (writes research_results.md)."""
import os
from pathlib import Path

import requests

out = []
tok = os.environ.get("OANDA_API_TOKEN", "").strip()
if not tok:
    out.append("OANDA_API_TOKEN secret: MISSING")
else:
    r = requests.get("https://api-fxpractice.oanda.com/v3/instruments/EUR_USD/candles",
                     params={"count": 2, "granularity": "H1"},
                     headers={"Authorization": f"Bearer {tok}"}, timeout=20)
    out.append(f"OANDA token: HTTP {r.status_code} {'OK' if r.ok else r.text[:200]}")
hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
if not hook:
    out.append("DISCORD_WEBHOOK_URL secret: MISSING")
else:
    r = requests.get(hook, timeout=20)
    out.append(f"Discord webhook: HTTP {r.status_code} {'OK' if r.ok else r.text[:200]}")
print("\n".join(out))
Path("research_results.md").write_text("\n".join(out))
