"""Download price history for strategy research (saved as gzip CSV)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402

days = int(sys.argv[1]) if len(sys.argv) > 1 else 730
out = Path("data")
out.mkdir(exist_ok=True)
data = bot.OandaData(os.environ["OANDA_API_TOKEN"].strip())
for inst in bot.INSTRUMENTS:
    df = data.history(inst, "M15", days)
    df.to_csv(out / f"{inst}_M15.csv.gz", compression="gzip")
    print(inst, len(df), df.index[0], df.index[-1])
