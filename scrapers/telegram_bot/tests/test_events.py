"""Tests for EVENT messages: recognising them, reading them into the event schema, when they stop being shown, and storing."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]          # internScraper/
sys.path.insert(0, str(ROOT))

from scrapers.telegram_bot import bot, events  # noqa: E402
from scrapers.telegram_bot.fetcher import Page, find_image  # noqa: E402
from scrapers.telegram_bot.parser import parse_post  # noqa: E402

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)      # 11:30 IST on 19 Oct 2026

PAYTM = """EVENT
Paytm Build for India AI Hackathon – Mumbai Edition
Date: 31st October 2026
Venue: Paytm Office, Mumbai
Top-performing participants get hiring opportunity with Paytm
Submit your pitch by 25th October 2026
https://devsemble.com/hackathons/paytm-build-for-india?utm_source=wa
"""


def fake_llm(**fields):
    base = {"title": "Paytm Build for India AI Hackathon", "organiser": "Paytm", "type": "hackathon", "mode": "offline",
            "location": "Mumbai", "start_date": "2026-10-31", "end_date": "2026-10-31", "registration_deadline": "2026-10-25",
            "registration_link": "https://devsemble.com/hackathons/paytm-build-for-india", "summary": "An 8-hour AI hackathon.",
            "perks": ["Hiring opportunity with Paytm"], "hiring_opportunity": True, "eligibility": None, "note": "Teams of 2-4"}
    base.update(fields)
    return lambda prompt: "```json\n" + json.dumps(base) + "\n```"


def utc(dt):
    """Mongo returns naive UTC datetimes; the code under test builds aware ones."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def page(ok=True, reason=None, image="https://cdn.example/banner.png"):
    return Page(url="https://devsemble.com/x", ok=ok, reason=reason, status=200 if ok else 404, text="Hackathon page text " * 20, image=image)


# ─── recognising the message ──────────────────────────────────────────────────

@pytest.mark.parametrize("text, body", [
    ("EVENT\nTCS Codevita\nhttps://x.example", "TCS Codevita\nhttps://x.example"),
    ("EVENT Meta Hacker Cup https://x.example", "Meta Hacker Cup https://x.example"),
    ("Event: Flipkart Grid 8.0", "Flipkart Grid 8.0"),
    ("event\nAdobe hackathon", "Adobe hackathon"),
    ("  EVENT - Google Big Code", "Google Big Code"),
])
def test_event_prefix_is_recognised(text, body):
    assert events.strip_event_prefix(text) == body


@pytest.mark.parametrize("text", [
    "Event Management Intern at Acme\nhttps://x.example",      # an internship post that happens to start with the word
    "Events team hiring interns",
    "EVENTS in Pune",
    "Company: IBM\nRole: EVENT coordinator",
    "",
])
def test_other_messages_are_not_events(text):
    assert events.strip_event_prefix(text) is None
    assert not events.is_event_message(text)


# ─── when an event stops being shown ──────────────────────────────────────────

def test_deadline_decides_and_counts_the_whole_day_in_ist():
    expires, known = events.compute_expiry("2026-10-25", "2026-10-31", "2026-10-31", NOW)
    assert known
    assert expires == datetime(2026, 10, 25, 18, 30, tzinfo=timezone.utc)      # 26 Oct 00:00 IST


def test_end_date_then_start_date_are_the_fallbacks():
    assert events.compute_expiry(None, "2026-11-02", "2026-11-01", NOW)[0] == events.end_of_day_ist("2026-11-02")
    assert events.compute_expiry(None, None, "2026-11-01", NOW)[0] == events.end_of_day_ist("2026-11-01")


def test_no_date_shows_for_thirty_days():
    expires, known = events.compute_expiry(None, None, None, NOW)
    assert not known and expires == NOW + timedelta(days=30)


# ─── reading the message ──────────────────────────────────────────────────────

def test_extract_event_fills_the_schema():
    body = events.strip_event_prefix(PAYTM)
    ev, err = events.extract_event(parse_post(body), body, page(), fake_llm(), NOW)
    assert err is None
    assert ev["title"] == "Paytm Build for India AI Hackathon" and ev["organiser"] == "Paytm"
    assert ev["type"] == "hackathon" and ev["mode"] == "offline" and ev["location"] == "Mumbai"
    assert ev["registrationDeadline"] == "2026-10-25" and ev["datesKnown"] is True
    assert ev["expiresAt"] == events.end_of_day_ist("2026-10-25")
    assert ev["hiring"] is True and ev["note"] == "Teams of 2-4"
    assert ev["imageUrl"] == "https://cdn.example/banner.png"
    assert ev["link"].startswith("https://devsemble.com/hackathons/paytm-build-for-india")


def test_bad_values_from_the_model_are_normalised():
    body = "Some contest https://x.example/c"
    ev, _ = events.extract_event(parse_post(body), body, None, fake_llm(type="Coding Sprint", mode="remote",
                                 start_date="soon", end_date="2026-13-45", registration_deadline=None, hiring_opportunity=False,
                                 perks=["Internship for winners"]), NOW)
    assert ev["type"] == "other" and ev["mode"] is None
    assert ev["startDate"] is None and ev["endDate"] is None and ev["datesKnown"] is False
    assert ev["hiring"] is True                   # inferred from the perk
    assert ev["imageUrl"] is None                 # no readable page, no image


def test_deadline_line_in_the_message_is_used_when_the_model_found_none():
    body = "Hack the Planet\nDeadline: 2026-11-10\nhttps://x.example/h"
    ev, _ = events.extract_event(parse_post(body), body, None, fake_llm(registration_deadline=None, start_date=None, end_date=None), NOW)
    assert ev["registrationDeadline"] == "2026-11-10" and ev["datesKnown"]


def test_event_without_a_name_is_refused():
    body = "https://x.example"
    ev, err = events.extract_event(parse_post(body), body, None, fake_llm(title=None), NOW)
    assert ev is None and "event name" in err


def test_registration_link_must_come_from_the_message_or_page():
    body = "Hack https://a.example/h https://b.example/reg"
    ev, _ = events.extract_event(parse_post(body), body, None, fake_llm(registration_link="https://b.example/reg/"), NOW)
    assert ev["link"] == "https://b.example/reg"
    ev, _ = events.extract_event(parse_post(body), body, None, fake_llm(registration_link=None), NOW)
    assert ev["link"] == "https://a.example/h"


def test_link_key_ignores_scheme_www_tracking_and_slash():
    assert events.link_key("https://www.Unstop.com/h/adobe/?utm_source=wa#top") == events.link_key("http://unstop.com/h/adobe")
    assert events.link_key("https://x.example/e?id=1") != events.link_key("https://x.example/e?id=2")


def test_share_image_is_made_absolute():
    html = '<meta property="og:image" content="/img/banner.png"><meta name="twitter:image" content="https://t.example/t.png">'
    assert find_image(html, "https://site.example/events/1") == "https://site.example/img/banner.png"
    assert find_image('<meta name="twitter:image" content="https://t.example/t.png">', "https://s.example") == "https://t.example/t.png"
    assert find_image("<p>nothing</p>", "https://s.example") is None
    assert find_image('<meta property="og:image" content="javascript:alert(1)">', "https://s.example") is None


# ─── storing ──────────────────────────────────────────────────────────────────

mongomock = pytest.importorskip("mongomock")


@pytest.fixture()
def col():
    return mongomock.MongoClient()["ifind"]["events"]


def run(col, body=PAYTM, llm=None, fetch=lambda url: page(), now=NOW):
    return bot.process_event(events.strip_event_prefix(body), fetch=fetch, llm=llm or fake_llm(),
                             push=lambda ev: events.save_event(col, ev, "42", now), now=now)


def test_event_is_saved_live_with_everything(col):
    (reply,) = run(col)
    assert reply.startswith("🎉 Saved event") and "Paytm" in reply and "25 Oct 2026" in reply
    doc = col.find_one({})
    assert doc["hidden"] is False and doc["source"] == "telegram" and doc["submittedBy"] == "42"
    assert utc(doc["expiresAt"]) == events.end_of_day_ist("2026-10-25")


def test_resending_updates_instead_of_duplicating(col):
    run(col)
    col.update_one({}, {"$set": {"hidden": True}})                       # a moderator hid it
    (reply,) = run(col, llm=fake_llm(registration_deadline="2026-10-28", note="Teams of 2-5"))
    assert reply.startswith("🔄 Updated event")
    assert col.count_documents({}) == 1
    doc = col.find_one({})
    assert doc["registrationDeadline"] == "2026-10-28" and doc["note"] == "Teams of 2-5"
    assert utc(doc["expiresAt"]) == events.end_of_day_ist("2026-10-28")
    assert doc["hidden"] is True                                         # a resend doesn't undo the moderator


def test_same_title_and_organiser_match_even_with_a_new_link(col):
    run(col)
    run(col, body="EVENT\nhttps://other.example/paytm", llm=fake_llm(registration_link="https://other.example/paytm"))
    assert col.count_documents({}) == 1


def test_resend_without_dates_keeps_the_stored_dates(col):
    run(col)
    run(col, llm=fake_llm(start_date=None, end_date=None, registration_deadline=None))
    doc = col.find_one({})
    assert doc["registrationDeadline"] == "2026-10-25" and doc["datesKnown"] is True
    assert utc(doc["expiresAt"]) == events.end_of_day_ist("2026-10-25")


def test_event_without_dates_is_saved_and_the_reply_says_so(col):
    body = "EVENT\nFlipkart Grid 8.0\nhttps://mycareernet.co/events/flipkart"
    (reply,) = run(col, body, llm=fake_llm(title="Flipkart GRID 8.0", organiser="Flipkart", start_date=None, end_date=None,
                                           registration_deadline=None))
    assert "no dates found" in reply and "Deadline:" in reply
    assert utc(col.find_one({})["expiresAt"]) == NOW + timedelta(days=30)


def test_event_that_already_ended_is_not_saved(col):
    (reply,) = run(col, llm=fake_llm(registration_deadline="2026-10-10", end_date="2026-10-12", start_date="2026-10-12"))
    assert "already ended" in reply and col.count_documents({}) == 0


def test_deadline_today_is_still_open(col):
    (reply,) = run(col, llm=fake_llm(registration_deadline="2026-10-19"))
    assert reply.startswith("🎉 Saved event")


def test_message_without_a_link_is_refused(col):
    (reply,) = run(col, body="EVENT\nSome hackathon next week, DM me")
    assert "no registration link" in reply and col.count_documents({}) == 0


def test_dead_page_refuses_but_a_login_wall_falls_back_to_the_message(col):
    (reply,) = run(col, fetch=lambda url: page(ok=False, reason="closed"))
    assert reply.startswith("❌ Not saved") and col.count_documents({}) == 0
    (reply,) = run(col, fetch=lambda url: page(ok=False, reason="login_wall"))
    assert reply.startswith("🎉 Saved event")
    assert col.find_one({})["imageUrl"] is None


def test_budget_stop_is_reported_and_nothing_is_saved(col, monkeypatch):
    monkeypatch.setattr(bot.usage_log, "budget_exceeded", lambda: True)
    (reply,) = bot.process_event(events.strip_event_prefix(PAYTM), fetch=lambda u: page(), llm=None,
                                 push=lambda ev: events.save_event(col, ev), now=NOW)
    assert "budget" in reply and col.count_documents({}) == 0


def test_event_messages_skip_the_internship_flow(monkeypatch):
    sent, internship_posts = [], []
    monkeypatch.setattr(bot, "send_message", lambda chat, text: sent.append(text))
    monkeypatch.setattr(bot, "process_event", lambda body, **kw: ["🎉 Saved event: x"])
    monkeypatch.setattr(bot, "process_post", lambda text, **kw: internship_posts.append(text) or ([], False))
    bot.process_message(1, "EVENT\nHack https://x.example")
    assert sent == ["🎉 Saved event: x"] and internship_posts == []
