# NHL Injury News Alert Bot

Posts [Daily Faceoff's NHL injury news](https://www.dailyfaceoff.com/hockey-player-news/injuries) to Slack
within about five minutes of each item appearing, oldest first. Each alert shows the player, a status read from the headline
(🔴 out · 🟡 might play · 🟢 playing or close), the headline and background, any timeline
("2-4 weeks ≈ Oct 13 – Oct 27"), where he stands on the CBS injury report, and a link to the
reporter's post.

```
🔴 Brock Faber  D · MIN  —  Out indefinitely · Surgery
> Faber underwent surgery to repair a fractured skull and is out indefinitely.
  Minnesota's top-pair defenseman suffered a scary skull fracture in the Wild's final preseason game…
  CBS: Upper Body — IR. Expected to be out until at least Oct 6
  via Michael Russo · posted 12:22 PM EDT · alert +4m · Daily Faceoff
```

About two-thirds of items are edited after posting, usually within the hour. An edit that changes
the status, timeline or surgery flag is posted again as `✏️ Update (Out → IR)`. Other edits
(typos, a longer background) are recorded but not re-posted.

The scraper, the headline parser and the CBS data come from the
[NHL Injury Database](https://github.com/ajpayzant/NHL-Injury-Tool) (`nhl_injuries/dfo.py`,
`data/injuries.csv`). The workflow checks that repo out into `injury-db/` on every run, so parser
fixes there reach the bot automatically.

## Setup

1. **Injury database.** The bot imports `nhl_injuries.dfo` from NHL-Injury-Tool's `main` branch,
   so changes to the parser there go live on the bot's next run.
2. **Repo permissions.** In this repo, under *Settings → Actions → General → Workflow permissions*,
   choose *Read and write*.
3. **Slack.** Add an Incoming Webhook for the injury channel (in the same Slack app as the line bot
   or a new one) and save it as the repo secret `SLACK_WEBHOOK_URL`.
4. **Optional repo variables** (*Settings → Secrets and variables → Actions → Variables*):
   - `INJURY_APP_URL`: the injury app's Streamlit URL. It adds an "Injury history" link to each alert.
   - `TEAMS`: e.g. `TOR,MTL` to only alert for those teams.
5. **Try it.** *Actions → NHL Injury News Alerts → Run workflow*:
   - Run `test-slack` to check the webhook.
   - Run `send-latest` to post the newest real item.
   - Run `check` once. The first run records what's on the page without posting.
6. **Every 5 minutes.** GitHub throttles the `*/5` schedule, so add a cron-job.org job like the line
   bot's:
   - `POST https://api.github.com/repos/ajpayzant/nhl-injury-bot/actions/workflows/nhl_injury_alerts.yml/dispatches`
   - Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`
   - Body: `{"ref":"main","inputs":{"mode":"check","send_backfill_on_first_run":"false"}}`

   The token needs *Actions: read and write* on this repo. If the line bot's fine-grained token is
   limited to that repo, add this one to it.

## Modes

| Mode | Does |
|---|---|
| `check` | Sends new items and news-changing edits, then commits `seen_injury_news.json` and `injury_news_alert_log.csv`. |
| `preview` | Prints what `check` would send; sends and saves nothing. |
| `scrape-only` | Prints the latest 10 items as messages. |
| `send-latest` | Sends the newest item without touching the seen file. |
| `test-slack` | Sends a test message. |

Safety rails: at most 15 alerts per run, and items more than 12 hours old are never sent (e.g. after downtime).
If a send fails, the run stops there and the item is retried first next run. Seen items are pruned after 7 days.

## Run locally

```
pip install -r requirements.txt pytest
set INJURY_DB_DIR=C:\path\to\NHL_Injury_Database
python injury_news_alert.py --mode preview
python -m pytest
```
