# Gold & FX Signal Bot (Discord)

ManaraFX-style trading pings for **gold (XAUUSD), silver (XAGUSD), EURUSD and GBPUSD**, sent to your Discord. It runs free on GitHub, so your PC can be off.

Each ping gives you **Entry, SL, TP1, TP2 and TP3**, plus a lot size for your account and the reasons for the trade. The bot then follows the trade and posts updates:

- ✅ **TP1 hit:** close ⅓ and move your SL to entry. The rest is now risk-free.
- ✅✅ **TP2 hit:** close another ⅓ and move your SL to TP1.
- 🎯 **TP3 hit:** trade complete.
- ❌ **SL hit**, ➖ **back to entry**, or ⏱️ **open 72h, close it**.
- 📊 A weekly summary every Friday evening.

> Not financial advice. Past results don't guarantee future results.

---

## The strategy

The rules were chosen from a test of 60+ rule sets on 2 years of real prices:

- **Trend:** only trade with the daily trend (price and the 20-day average on the same side of the 50-day average).
- **Entry:** a 1-hour candle closes beyond the **high or low of the last 48 hours**, between 07:00 and 19:59 UK time.
- **Stop loss:** 2× the hourly ATR (average hourly range), so it's wider when the market is moving more.
- **Targets:** TP1 / TP2 / TP3 at 1.5× / 3× / 4.5× the risk, closing a third at each.

**2-year backtest on real prices (Oct 2024 – Oct 2026):**

| | Signals | Won | Profit factor | Total | Worst dip |
|---|---|---|---|---|---|
| Gold | 103 | 53% | 1.99 | +48.5R | 5.6R |
| Silver | 98 | 42% | 1.25 | +14.8R | 10.1R |
| EURUSD | 99 | 42% | 1.20 | +11.7R | 8.5R |
| GBPUSD | 102 | 42% | 1.12 | +7.1R | 6.9R |
| **All four** | **402** | **45%** | **1.36** | **+82.2R** | **19.0R** |

That's about **4 pings a week**, and both years were profitable (+40.4R, then +41.7R). R = the amount you risk on one trade.

**Be realistic about these numbers:**
- **Most of the profit came from gold**, during its big 2024–26 rally. A long trend like that won't last forever.
- The same rules were tested on 20 markets. Most **lost money or only worked in one year** (USDJPY, AUDUSD, EURGBP, indices, oil), so they're switched off.
- I picked these four markets *after* seeing the results, so real trading will likely do somewhat worse. The bot tracks its live record, so you'll know for sure.

---

## What £50 can do

The average stop is about **$27 on gold, 28 pips on EURUSD and 34 pips on GBPUSD**. At the smallest size (0.01 lots):
- **Gold and silver** risk about **£20+** per trade and need more margin than £50, so they're **watch/demo only**.
- **EURUSD / GBPUSD** risk about **£2–2.60** per trade (4–5% of £50). That's above the bot's 3% safety limit, so those pings also arrive **orange = watch/demo only**.

With **£100 or more**, the forex pings fit at sensible risk. Update `account_balance_gbp` in `config.json` when your balance changes. **Demo-trade the pings for at least a month first.**

---

## Running it (GitHub, free)

Everything is already set up in your repository. See **PHONE_SETUP.md** for the full steps. In short:

1. Your two secrets must be added: `DISCORD_WEBHOOK_URL` and `OANDA_API_TOKEN`.
2. **Actions → Signal check → Enable workflow.** This checks the market every 15 minutes, Sunday to Friday.
3. Optional: **Actions → Bot tools → Run workflow → backtest** posts the latest backtest to Discord.

To run it on a PC instead, install Python, run `pip install -r requirements.txt`, put your webhook and token in `config.json`, then run `python bot.py run` (or double-click `start_bot.bat`).

---

## How to use a ping

1. **Orange = watch/demo only**: the trade is too big for your account balance.
2. **Don't chase.** If price is already past the "Don't chase" level, skip it.
3. Set the **SL straight away**. If your size allows, split it into 3 parts with TP1, TP2 and TP3.
4. Follow the update messages: SL to entry at TP1, SL to TP1 at TP2.
5. Check the news calendar (forexfactory.com). The bot can't see news events.

---

## Settings (`config.json`)

To edit on GitHub: open the file, tap ✏️, change it, then **Commit changes**.

| Setting | What it does |
|---|---|
| `instruments` | Markets to watch (default gold, silver, EURUSD, GBPUSD) |
| `account_balance_gbp` | Your account size. It sets the lot size, so keep it updated |
| `risk_percent` | % of the account risked per trade (2 is sensible) |
| `max_risk_percent` | Pings that would risk more than this, even at 0.01 lots, are marked orange |
| `send_signals_too_big_for_account` | `false` = don't send orange pings at all |
| `take_profits_r` | TP1/TP2/TP3 as multiples of the risk |
| `max_signals_per_day` | Cap on pings per day |
| `session_start_hour_uk` / `session_end_hour_uk` | Hours (UK time) when new signals can fire |
