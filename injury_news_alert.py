# ============================================================
# Daily Faceoff NHL Injury News Alert Bot
# GitHub Actions Production Version
# ============================================================
#
# What it does:
#   1. Reads https://www.dailyfaceoff.com/hockey-player-news/injuries
#   2. Detects new injury news items (and edits that change the news)
#   3. Reads each headline for a status, timeline and surgery flag, and looks
#      the player up on the CBS injury report, using the NHL Injury Database
#      code and data (ajpayzant/NHL-Injury-Tool, checked out next to this repo)
#   4. Sends the alerts to Slack, oldest first
#   5. Saves seen items and an alert log for future runs
#
# Required GitHub Secret:
#   SLACK_WEBHOOK_URL
#
# Files used:
#   seen_injury_news.json
#   injury_news_alert_log.csv
# ============================================================

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# The NHL Injury Database repo: Daily Faceoff scraper, headline parser, CBS data.
INJURY_DB_DIR = Path(os.getenv("INJURY_DB_DIR", "injury-db")).resolve()
sys.path.insert(0, str(INJURY_DB_DIR))
from nhl_injuries import dfo  # noqa: E402


# ============================================================
# CONFIG
# ============================================================

SEEN_PATH = "seen_injury_news.json"
ALERT_LOG_PATH = "injury_news_alert_log.csv"

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()

# Safety cap so one run cannot accidentally blast Slack with too many messages.
MAX_NEW_ALERTS_PER_RUN = int(os.getenv("MAX_NEW_ALERTS_PER_RUN", "15"))

# Seconds between Slack sends.
REQUEST_SLEEP_SECONDS = float(os.getenv("REQUEST_SLEEP_SECONDS", "0.75"))

# Items posted longer ago than this are never alerted (e.g. after the bot was down).
MAX_ITEM_AGE_HOURS = float(os.getenv("MAX_ITEM_AGE_HOURS", "12"))

# An edit that changes the status, timeline or surgery flag is re-sent as an
# update if the item is younger than this. Other edits are recorded silently.
UPDATE_WINDOW_HOURS = float(os.getenv("UPDATE_WINDOW_HOURS", "6"))

# Optional: only alert for these teams, e.g. "TOR,MTL". Empty = every team.
TEAMS = {t.strip().upper() for t in os.getenv("TEAMS", "").split(",") if t.strip()}

# Optional: the injury database app, for a link to the player's injury history.
INJURY_APP_URL = os.getenv("INJURY_APP_URL", "").strip().rstrip("/")

# Seen items older than this are pruned.
MAX_SEEN_AGE_DAYS = 7

# Pages read per run: page 1 has the latest 20 items (about a day in season).
# Older pages are read only while every item on the page is new.
MAX_PAGES = 3

ET = ZoneInfo("America/New_York")
DFO_URL = dfo.BASE_URL
EMOJI = {"bad": "🔴", "warn": "🟡", "good": "🟢", "": "⚪"}


# ============================================================
# BASIC HELPERS
# ============================================================

def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def format_et(utc_dt: datetime, with_date: bool = True) -> str:
    """e.g. "Sep 29 at 12:22 PM EDT". Built by hand because %-d / %-I are not
    supported on Windows."""
    et_dt = utc_dt.astimezone(ET)
    hour = et_dt.hour % 12 or 12
    clock = f"{hour}:{et_dt:%M} {et_dt:%p} {et_dt.tzname()}"
    return f"{et_dt:%b} {et_dt.day} at {clock}" if with_date else clock


def short_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def age_hours(ts: str) -> float:
    return (now_utc() - parse_utc(ts)).total_seconds() / 3600


def slack_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_chars else text[:max_chars].rsplit(" ", 1)[0] + "…"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        print(f"WARNING: {path} not found; alerts will not include the CBS report.")
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ============================================================
# SEEN-ITEM STORAGE
# ============================================================
# news_id -> what was last sent (or recorded) for that item, so edits can be told
# apart from new items and only news-changing edits are re-sent.

def load_seen(path: str = SEEN_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        print(f"WARNING: Could not parse {path}. Treating as empty.")
        return {}


def save_seen(seen: dict, path: str = SEEN_PATH):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(sorted(seen.items())), f, indent=2)
        f.write("\n")


def seen_record(row: dict) -> dict:
    return {"published_at": row["published_at"], "updated_at": row["updated_at"],
            "status": row["news_status"], "timeline": row["timeline"],
            "surgery": bool(row["surgery"]), "headline": row["headline"]}


def prune_seen(seen: dict, max_age_days: int = MAX_SEEN_AGE_DAYS) -> dict:
    keep = {k: v for k, v in seen.items() if age_hours(v["published_at"]) <= max_age_days * 24}
    if len(keep) < len(seen):
        print(f"Pruned {len(seen) - len(keep)} seen items older than {max_age_days} days.")
    return keep


def news_changed(old: dict, row: dict) -> bool:
    return (old["status"], old["timeline"], old["surgery"]) != (
        row["news_status"], row["timeline"], bool(row["surgery"]))


# ============================================================
# DAILY FACEOFF + CBS
# ============================================================

def fetch_items(seen: dict, max_pages: int = MAX_PAGES) -> list[dict]:
    client = dfo.Client(pause=0.5)
    items: list[dict] = []
    for n in range(1, max_pages + 1):
        page, last_page = client.page(n)
        items.extend(page)
        # Keep going only if the whole page was new (a burst of news or a long gap).
        if not seen or not page or n >= last_page or any(str(i["id"]) in seen for i in page):
            break
    return items


def load_rows(seen: dict, max_pages: int = MAX_PAGES) -> tuple[list[dict], dict[str, dict]]:
    """Parsed Daily Faceoff items, oldest first, and the open CBS injury per NHL id."""
    stamp = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [dfo.to_row(i, stamp) for i in fetch_items(seen, max_pages)]
    rows = list({r["news_id"]: r for r in rows}.values())
    injuries = read_csv(INJURY_DB_DIR / "data" / "injuries.csv")
    players = read_csv(INJURY_DB_DIR / "data" / "nhl" / "players.csv")
    dfo.refresh(rows, injuries, players, datetime.now(ET).date())
    open_cbs = {i["nhl_id"]: i for i in injuries if i.get("nhl_id") and not i.get("return_date")}
    return sorted(rows, key=lambda r: r["published_at"]), open_cbs


# ============================================================
# SLACK
# ============================================================

def validate_slack_webhook():
    if not SLACK_WEBHOOK_URL:
        raise ValueError("Missing SLACK_WEBHOOK_URL environment variable / GitHub Secret.")
    if not SLACK_WEBHOOK_URL.startswith("https://hooks.slack.com/services/"):
        raise ValueError("SLACK_WEBHOOK_URL does not look like a valid Slack Incoming Webhook URL.")


def send_slack_message(payload: dict):
    validate_slack_webhook()
    response = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"Slack send failed. Status={response.status_code}, Body={response.text}")


def timeline_text(row: dict) -> str:
    if not row["timeline"]:
        return ""
    early, late = row["return_earliest"], row["return_latest"]
    if late and late != early:
        when = f"{short_date(early)} – {short_date(late)}"
    elif early:
        when = ("after " if row["timeline"].startswith("at least") else "") + short_date(early)
    else:
        when = ""
    return f"{row['timeline']}" + (f" (≈ {when})" if when else "")


def cbs_text(row: dict, open_cbs: dict[str, dict]) -> str:
    injury = open_cbs.get(row["nhl_id"]) if row["nhl_id"] else None
    if injury:
        return f"{injury['injury_type']} — {injury['status']}"
    return "not on the report"


def build_slack_notification(row: dict, open_cbs: dict[str, dict], old: dict | None = None) -> dict:
    """Block Kit message for one item; ``old`` is the previously sent version for an update."""
    tone = dfo.TONE.get(row["news_status"], "")
    status = row["news_status"] if row["news_status"] != "Other" else ""
    who = " · ".join(x for x in (row["position"], row["team"]) if x)

    title = f"{EMOJI[tone]} *{slack_escape(row['player'])}*" + (f"  {who}" if who else "")
    tags = [f"*{status}*"] if status else []
    if row["surgery"]:
        tags.append("Surgery")
    if old is not None:
        change = (f"{old['status']} → {row['news_status']}" if old["status"] != row["news_status"]
                  else "timeline or details changed")
        tags.insert(0, f"✏️ Update ({slack_escape(change)})")
    if tags:
        title += "  —  " + " · ".join(tags)

    posted = parse_utc(row["published_at"])
    delay = max(0, int((now_utc() - (parse_utc(row["updated_at"]) if old else posted)).total_seconds() // 60))
    source = (f"<{row['source_url']}|{slack_escape(row['source_name'] or 'source')}>"
              if row["source_url"] else slack_escape(row["source_name"]))
    facts = []
    if timeline_text(row):
        facts.append(f"*Timeline:* {slack_escape(timeline_text(row))}")
    facts.append(f"*CBS:* {slack_escape(cbs_text(row, open_cbs))}")
    meta = [f"via {source}" if source else "", f"posted {format_et(posted, with_date=False)}",
            f"alert +{delay}m"]
    links = [f"<{DFO_URL}|Daily Faceoff>"]
    if INJURY_APP_URL and row["nhl_id"]:
        links.insert(0, f"<{INJURY_APP_URL}/players?player={row['nhl_id']}|Injury history>")

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "> " + slack_escape(row["headline"])}},
    ]
    if row["context"]:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": slack_escape(truncate(row["context"], 600))}]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": "   ".join(facts)},
        {"type": "mrkdwn", "text": " · ".join(x for x in meta + links if x)},
    ]})

    fallback = f"{row['player']}" + (f" ({row['team']})" if row["team"] else "") + \
               (f" — {status}" if status else "") + f": {row['headline']}"
    return {"text": ("Update: " if old else "") + fallback, "blocks": blocks,
            "unfurl_links": False, "unfurl_media": False}


def build_test_message() -> dict:
    return {"text": (
        "🩹 *NHL Injury News Bot Test*\n\n"
        "This is a test message from GitHub Actions.\n\n"
        "If this appears in your Slack channel, the webhook connection is working.\n\n"
        f"Timestamp: {format_et(now_utc())}")}


def message_preview(payload: dict) -> str:
    """Plain-text rendering of a payload for scrape-only / preview runs."""
    lines = []
    for b in payload.get("blocks", []):
        if b["type"] == "section":
            lines.append(b["text"]["text"])
        else:
            lines.extend("   " + e["text"] for e in b["elements"])
    return "\n".join(lines) or payload["text"]


# ============================================================
# ALERT LOGGING
# ============================================================

LOG_FIELDS = ["alert_sent_at", "kind", "news_id", "published_at", "updated_at", "player", "team",
              "nhl_id", "news_status", "timeline", "surgery", "cbs", "source_name", "source_url", "headline"]


def append_alert_log(sent: list[tuple[str, dict, str]], path: str = ALERT_LOG_PATH):
    if not sent:
        return
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, lineterminator="\n")
        if new_file:
            writer.writeheader()
        for kind, row, cbs in sent:
            writer.writerow({
                "alert_sent_at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind,
                **{k: row[k] for k in ("news_id", "published_at", "updated_at", "player", "team", "nhl_id",
                                       "news_status", "timeline", "surgery", "source_name", "source_url",
                                       "headline")},
                "cbs": cbs})


# ============================================================
# MAIN CHECK
# ============================================================

def pick_alerts(rows: list[dict], seen: dict) -> list[tuple[str, dict]]:
    """("new" | "update", row) pairs to send, in the order they happened."""
    out = []
    for r in rows:
        if TEAMS and r["team"] not in TEAMS:
            continue
        old = seen.get(r["news_id"])
        if old is None:
            if age_hours(r["published_at"]) <= MAX_ITEM_AGE_HOURS:
                out.append(("new", r))
        elif (r["updated_at"] != old["updated_at"] and news_changed(old, r)
              and age_hours(r["published_at"]) <= UPDATE_WINDOW_HOURS):
            out.append(("update", r))
    return sorted(out, key=lambda a: a[1]["updated_at"] if a[0] == "update" else a[1]["published_at"])


def check_and_send_alerts(send_backfill_on_first_run: bool = False, preview: bool = False) -> int:
    seen = prune_seen(load_seen())
    rows, open_cbs = load_rows(seen)
    print(f"Items read: {len(rows)} · previously seen: {len(seen)}")
    if not rows:
        return 0

    if not seen and not send_backfill_on_first_run and not preview:
        for r in rows:
            seen[r["news_id"]] = seen_record(r)
        save_seen(seen)
        print(f"First run detected. Saved {len(rows)} current items as seen. No Slack alerts sent.")
        return 0

    alerts = pick_alerts(rows, seen)
    print(f"Alerts before cap: {len(alerts)} "
          f"({sum(k == 'new' for k, _ in alerts)} new, {sum(k == 'update' for k, _ in alerts)} updates)")
    if len(alerts) > MAX_NEW_ALERTS_PER_RUN:
        print(f"Safety cap active: limiting alerts from {len(alerts)} to {MAX_NEW_ALERTS_PER_RUN} this run.")
        alerts = alerts[:MAX_NEW_ALERTS_PER_RUN]

    sent = []
    for kind, row in alerts:
        payload = build_slack_notification(row, open_cbs, seen.get(row["news_id"]) if kind == "update" else None)
        if preview:
            print(f"\n--- {kind} ---\n{message_preview(payload)}")
            continue
        try:
            send_slack_message(payload)
        except Exception as e:
            # Stop here so a later item is never posted ahead of this one.
            # It is not marked seen, so the next run retries it first.
            print(f"ERROR: Failed to send Slack alert for {row['news_id']} ({row['player']}): {e}")
            break
        print(f"Slack {kind} sent for {row['news_id']} ({row['player']})")
        sent.append((kind, row, cbs_text(row, open_cbs)))
        # Save after every send so a crash later in the run cannot cause repeats.
        seen[row["news_id"]] = seen_record(row)
        save_seen(seen)
        time.sleep(REQUEST_SLEEP_SECONDS)

    if preview:
        return 0

    # Record edits that weren't worth an alert, and items skipped as too old or
    # for another team, so they are compared against their latest version next time.
    alerted = {r["news_id"] for _, r in alerts}
    for r in rows:
        if r["news_id"] not in alerted:
            seen[r["news_id"]] = seen_record(r)
    save_seen(seen)
    append_alert_log(sent)
    print(f"Saved {len(seen)} seen items. Logged {len(sent)} sent alerts.")
    return len(sent)


# ============================================================
# CLI
# ============================================================

def parse_bool(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="check",
                        choices=["check", "preview", "test-slack", "scrape-only", "send-latest"],
                        help="check: send new alerts. preview: print what check would send, change nothing. "
                             "scrape-only: print the latest items as messages. send-latest: send the newest "
                             "item without touching seen items.")
    parser.add_argument("--send-backfill-on-first-run", default="false",
                        help="If true, sends current items as alerts when the seen file is empty.")
    args = parser.parse_args()

    if args.mode == "test-slack":
        send_slack_message(build_test_message())
        print("Slack test message sent.")
        return

    if args.mode in ("scrape-only", "send-latest"):
        rows, open_cbs = load_rows({}, max_pages=1)
        if not rows:
            print("No items found.")
            return
        if args.mode == "send-latest":
            send_slack_message(build_slack_notification(rows[-1], open_cbs))
            print(f"Slack alert sent for {rows[-1]['news_id']} ({rows[-1]['player']})")
            return
        for r in rows[-10:]:
            print(f"\n[{r['news_id']}] matched_by={r['matched_by']} injury={r['injury'] or '-'}")
            print(message_preview(build_slack_notification(r, open_cbs)))
        return

    print(f"Run started at {format_et(now_utc())} · mode {args.mode}")
    n = check_and_send_alerts(parse_bool(args.send_backfill_on_first_run), preview=args.mode == "preview")
    print(f"Run complete. Alerts sent: {n}")


if __name__ == "__main__":
    main()
