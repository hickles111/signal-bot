# Run the bot for free on GitHub (no PC needed)

GitHub runs the bot every 15 minutes on its own servers. Your phone just receives the Discord pings.
Everything below can be done on a phone. For the GitHub steps, use your phone's **web browser**, not the GitHub app.

## 1. Discord webhook (2 min)
1. Open the Discord app and go to your server.
2. Long-press the channel you want, e.g. `#signals`, and tap **Edit Channel**.
3. Tap **Integrations → Webhooks → New Webhook**.
4. Tap the new webhook, then **Copy Webhook URL**. Paste it into your Notes for now.
5. Make sure notifications are on for that channel.

## 2. Free price data: OANDA demo (5 min)
1. Go to oanda.com and sign up for a free **demo / practice** account. No deposit is needed.
2. Log in to the account area and find **API access** (it may be called "Manage API Access").
3. Generate a token and copy it into your Notes.

## 3. GitHub account and repository (3 min)
1. Sign up at github.com. It's free.
2. Tap **+ → New repository**.
3. Name it `signal-bot`, set it to **Public**, and tap **Create repository**.
   - It has to be public, because GitHub's servers are only free and unlimited for public repositories.
   - Your webhook and token stay hidden as "secrets". Only the code and the trade log are visible.

## 4. Put the bot in it
- **Easiest:** tell Claude your GitHub username. If Claude has access to your GitHub, it can upload everything for you.
- **By hand:** unzip the bot on your phone. In the repo, tap **Add file → Upload files** and upload `bot.py`, `config.json` and `requirements.txt`. Then tap **Add file → Create new file**, name it `.github/workflows/signals.yml`, and paste in that file's contents. Do the same for `.github/workflows/tools.yml`.

## 5. Add your two secrets
In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**. If you can't see Settings, switch your browser to "Desktop site". Add:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the Discord webhook URL from step 1 |
| `OANDA_API_TOKEN` | the OANDA token from step 2 |

## 6. Test it
1. Open the **Actions** tab. If GitHub asks, tap the button to enable workflows.
2. Tap **Bot tools → Run workflow**, choose `test-discord`, and run it. Your phone should get a ping.
3. Run **Bot tools** again with `backtest`. After a minute or two, the results on 90 days of real prices are posted to Discord.
   **If PF (profit factor) is under 1.3, don't trade it yet.** Send the numbers to Claude so the rules can be tuned.

That's it. The **Signal check** workflow now runs every 15 minutes, Sunday to Friday, by itself.

## Good to know
- GitHub sometimes starts runs a few minutes late, and very occasionally skips one. The bot catches up on missed candles, but it won't send signals older than 20 minutes, because price has probably moved by then.
- To change settings, open `config.json` in the repo, tap the pencil icon, edit, and commit. Remember to update `account_balance_gbp` as your balance changes.
- To pause the bot: **Actions → Signal check → ⋯ → Disable workflow**.
- GitHub may email you if a run fails. One-off failures are normal; the bot also warns you in Discord if it fails 4 checks in a row.
- Demo-trade the signals for 3–4 weeks before using real money.
