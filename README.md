# Gold & FX Signal Bot (Discord)

The bot watches Gold, EUR/USD, GBP/USD and USD/JPY on the 15-minute chart. It only pings Discord when a trend-pullback setup passes **every hard rule and at least 5 of 6 quality checks**. Most days you'll get 0–2 pings. That's on purpose.

Each ping tells you the entry, stop loss, take profit, lot size, how much you risk and stand to make in £, and why the trade was picked. Then it follows the trade for you:

- 🛡️ It tells you when to move your stop to entry, once the trade is +1R in profit. From then on, the trade can't lose.
- ✅ / ❌ It reports when the trade hits take profit or stop loss.
- ⏱️ If a trade is still open after 12 hours, it tells you to close it.
- 📊 Every Friday evening it posts a weekly summary.

> Not financial advice. No strategy wins every trade. This one aims to win about 2× what it loses on each trade, so it can be profitable while being right less than half the time.

---

## Honest numbers for a £50 account

- The bot risks **2% per trade (about £1)**, so a winning trade makes about **£2**.
- **Gold is too big for £50.** Under UK rules, the smallest gold trade (0.01 lots) needs over £60 of margin. Gold pings will arrive in **orange, marked "watch only"**. Forex pings in green or red are ones you can actually take.
- You can only hold **one trade at a time**, because one 0.01 lot forex trade uses about £25–30 of margin.
- Realistically, that's a few pounds a week **if the strategy works**. The real value of £50 is proving the system works before you put more in.

---

## Setup (about 15 minutes)

### 1. Install Python
Download Python from **python.org**. On Windows, **tick "Add Python to PATH"** during install.

### 2. Install the bot's packages
Open a terminal in this folder. On Windows, click the folder's address bar, type `cmd` and press Enter. Then run:
```
pip install -r requirements.txt
```

### 3. Make a Discord webhook (where pings go)
1. In your Discord server, open the channel you want, e.g. `#signals`.
2. Go to **Edit Channel → Integrations → Webhooks → New Webhook**.
3. Click **Copy Webhook URL**.
4. Open `config.json` and paste it in place of `PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE`.
5. Turn on notifications for that channel in the Discord app so your phone buzzes.

### 4. Get free real-time prices (OANDA demo)
1. Sign up for a free **demo / practice account** at oanda.com. You don't need to deposit anything.
2. In your account area, find **API access** (it may be called "Manage API Access") and generate a token.
3. Paste the token into `config.json` in place of `PASTE_YOUR_OANDA_DEMO_TOKEN_HERE`.

*No-signup alternative:* set `"data_source": "yahoo"`. That works for forex, but gold uses futures prices, which can be a few dollars off your broker's price. OANDA is better.

### 5. Test it
```
python bot.py test-discord     ← should ping your channel
python bot.py backtest         ← tests the strategy on the last 90 days of REAL prices
```
**Read the backtest before anything else.** It shows the win rate, total profit in R and £, the worst losing streak, and the profit factor.
- Profit factor **above 1.3** is decent.
- **Below 1.0** means it would have lost money. Don't trade it. Tell Claude, and the rules can be adjusted.

### 6. Run it
```
python bot.py run
```
On Windows you can just double-click `start_bot.bat`; on Mac, `start_bot.command`. Leave the window open. The PC must stay on and **not go to sleep** while the market is open (Sunday 10pm to Friday 10pm UK time). On Windows, go to **Settings → System → Power** and set sleep to "Never" while it runs.

To see how all the live signals have done so far:
```
python bot.py stats
```

---

## Run it without your PC (free)

See **PHONE_SETUP.md**. GitHub runs the bot every 15 minutes for free, and you can set it all up from your phone.

---

## How to use a ping

1. Check the colour first. **Orange = watch only**, so don't trade it with real money.
2. **Don't chase.** If price has already passed the "Don't chase" level, skip the trade.
3. Place the trade **with the stop loss and take profit set straight away**. Never trade without the stop.
4. When the 🛡️ message arrives, move your stop loss to your entry price.
5. Glance at the news calendar (forexfactory.com). Skip trades within 30 minutes of a red-folder news event for that currency. The bot can't see the news.

## The plan (please actually do this)

1. **Weeks 1–4: demo only.** Take every green or red ping on a demo account set to £50.
2. After 20+ signals, run `python bot.py stats`. If the total R is positive and the profit factor is above 1.3, go live with real £50 at the same 2% risk.
3. Only add money once live results match demo. **Never raise risk_percent to try to hit a daily target.** That's how small accounts get wiped.

---

## Settings (`config.json`)

| Setting | What it does |
|---|---|
| `instruments` | Markets to watch. Options: `XAU_USD`, `EUR_USD`, `GBP_USD`, `USD_JPY`, `AUD_USD`, `EUR_GBP` |
| `account_balance_gbp` | Your account size. **Update it as your balance changes**, because it sets the lot size |
| `risk_percent` | % of the account to risk per trade (2 = £1 on £50). 1–2 is sensible |
| `max_risk_percent` | Pings that would risk more than this, even at the smallest size, are marked "watch only" |
| `min_quality_score` | 5 = strict (default), 6 = only perfect setups (very few pings), 4 = more pings, lower quality |
| `max_signals_per_day` | Hard cap on pings per day |
| `reward_to_risk` | Take profit distance as a multiple of the stop (2 = aim for twice what you risk) |
| `session_start_uk` / `session_end_uk` | Only looks for trades between these UK times (London + New York sessions) |
| `send_signals_too_big_for_account` | `false` = hide the orange "watch only" pings completely |

## What the bot looks for

**Hard rules (all must pass):**
- The 1-hour trend is clear: price and the 50 EMA are on the same side of the 200 EMA.
- The 15-minute trend agrees: the 20 EMA is beyond the 50 EMA in the trend direction.
- Price pulled back to the 20 EMA within the last hour, then a candle closed back in the trend direction, past the previous candle.
- Volatility is normal. It skips dead markets and news spikes.
- The stop isn't too wide. It sits just past the pullback swing.
- The candle closed during London or New York hours.

**Quality checks (needs 5 of 6):** trend strength (ADX ≥ 20), healthy momentum (RSI not stretched), 1-hour momentum agrees, price not overstretched from the 50 EMA, a strong confirmation candle, and no recent high or low between entry and target.
