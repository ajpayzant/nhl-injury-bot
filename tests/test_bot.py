"""Offline tests: Daily Faceoff and Slack are stubbed out."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import injury_news_alert as bot


def ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(i, hours_ago=1.0, status="Out", headline="Faber (upper body) will not play Tuesday.", team="MIN",
        updated=None, timeline="", surgery=False):
    return {"news_id": str(i), "published_at": ts(hours_ago), "updated_at": updated or ts(hours_ago),
            "date": "2026-09-29", "player": "Brock Faber", "position": "D", "team": team,
            "nhl_id": "8482122", "headline": headline, "context": "Background <b>& more</b>.",
            "source_name": "Michael Russo", "source_url": "https://x.com/RussoHockey/status/1",
            "news_status": status, "timeline": timeline, "return_earliest": "", "return_latest": "",
            "surgery": surgery, "matched_by": "dfo", "injury": "Upper Body"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "REQUEST_SLEEP_SECONDS", 0)
    monkeypatch.setattr(bot, "TEAMS", set())
    state = {"rows": [], "sent": [], "fail_on": None}
    monkeypatch.setattr(bot, "load_rows", lambda seen, max_pages=3: (sorted(state["rows"], key=lambda r: r["published_at"]), {}))

    def send(payload):
        if state["fail_on"] and state["fail_on"] in payload["text"]:
            raise RuntimeError("Slack down")
        state["sent"].append(payload)
    monkeypatch.setattr(bot, "send_slack_message", send)
    return state


def seen_file():
    with open(bot.SEEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_first_run_records_without_sending(env):
    env["rows"] = [row(1), row(2)]
    assert bot.check_and_send_alerts() == 0
    assert env["sent"] == [] and set(seen_file()) == {"1", "2"}


def test_new_items_oldest_first_and_old_ones_skipped(env):
    env["rows"] = [row(1, 3)]
    bot.check_and_send_alerts()
    env["rows"] = [row(1, 3), row(3, 0.5, headline="Newest"), row(2, 1, headline="Older"), row(9, 20)]
    assert bot.check_and_send_alerts() == 2
    assert [p["text"].split(": ")[1] for p in env["sent"]] == ["Older", "Newest"]
    assert set(seen_file()) == {"1", "2", "3", "9"}   # the 20-hour-old item is recorded, not sent
    assert bot.check_and_send_alerts() == 0


def test_edits_only_resent_when_the_news_changes(env):
    env["rows"] = [row(1, 2)]
    bot.check_and_send_alerts()
    # Typo fix: recorded silently.
    env["rows"] = [row(1, 2, headline="Faber (upper-body) will not play Tuesday.", updated=ts(1.5))]
    assert bot.check_and_send_alerts() == 0
    assert seen_file()["1"]["headline"].startswith("Faber (upper-body)")
    # Now he's placed on IR: an update.
    env["rows"] = [row(1, 2, status="IR", headline="Faber has been placed on IR.", updated=ts(1))]
    assert bot.check_and_send_alerts() == 1
    assert env["sent"][0]["text"].startswith("Update: ")
    assert "Out → IR" in env["sent"][0]["blocks"][0]["text"]["text"]
    assert bot.check_and_send_alerts() == 0


def test_failed_send_stops_and_retries_next_run(env):
    env["rows"] = [row(1, 5)]
    bot.check_and_send_alerts()
    env["rows"] = [row(1, 5), row(2, 2, headline="Second"), row(3, 1, headline="Third")]
    env["fail_on"] = "Second"
    assert bot.check_and_send_alerts() == 0
    assert "2" not in seen_file() and "3" not in seen_file()
    env["fail_on"] = None
    assert bot.check_and_send_alerts() == 2
    assert [p["text"].split(": ")[1] for p in env["sent"]] == ["Second", "Third"]


def test_team_filter(env, monkeypatch):
    env["rows"] = [row(1, 5)]
    bot.check_and_send_alerts()
    monkeypatch.setattr(bot, "TEAMS", {"TOR"})
    env["rows"] = [row(1, 5), row(2, 1, team="MIN"), row(3, 1, team="TOR", headline="Leafs")]
    assert bot.check_and_send_alerts() == 1
    assert env["sent"][0]["text"].endswith("Leafs")


def test_message_escapes_and_shows_cbs():
    r = row(1, surgery=True, timeline="2-4 weeks")
    r.update(return_earliest="2026-10-13", return_latest="2026-10-27")
    msg = bot.build_slack_notification(r, {"8482122": {"injury_type": "Upper Body", "status": "IR"}})
    text = bot.message_preview(msg)
    assert "🔴 *Brock Faber*  D · MIN  —  *Out* · Surgery" in text
    assert "&lt;b&gt;&amp; more" in text
    assert "*Timeline:* 2-4 weeks (≈ Oct 13 – Oct 27)" in text and "*CBS:* Upper Body — IR" in text
    assert "not on the report" in bot.message_preview(bot.build_slack_notification(row(1), {}))
