"""Tests for the Telegram bot. Sample posts are shaped like real WhatsApp forwards (sender details removed)."""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]          # internScraper/
sys.path.insert(0, str(ROOT))

from scrapers.telegram_bot import bot  # noqa: E402
from scrapers.telegram_bot.extractor import company_from_email, company_from_url, extract_internships  # noqa: E402
from scrapers.telegram_bot.fetcher import Page, _classify, html_to_text  # noqa: E402
from scrapers.telegram_bot.parser import parse_post, split_posts  # noqa: E402

EXPORT = """[10/13, 11:38 PM] Sender: Posted: A Minute Ago

Company: IBM

Role: Software Engineering Interns

Location: Bangalore

- Eligibility: 2027 Batch Preferred
- Apply Link:
https://careers.ibm.com/en_US/careers/JobDetail/Intern-Associate-Engineer/137023
[10/13, 11:44 PM] Sender: #Mass Hiring
- Role : SDE Interns (CPU Complier)
- Location: Pune, Bangalore

https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/India-Pune/CPU-Compiler-Intern---2027_JR2027260-1?x=1
[10/14, 2:01 PM] Sender: Company: FOXO
💼 Role: AI Intern (2 Openings)
💰 Stipend: Up to ₹60,000/month
📩 How to Apply: Email your resume to careers@foxo.club
"""


def fake_llm(payload: dict):
    return lambda prompt: "```json\n" + json.dumps(payload) + "\n```"


# ─── parser ───────────────────────────────────────────────────────────────────

def test_export_is_split_on_whatsapp_headers():
    posts = split_posts(EXPORT)
    assert len(posts) == 3
    assert posts[0].startswith("Posted: A Minute Ago")
    assert "Sender" not in posts[1]


def test_plain_message_is_one_post():
    assert split_posts("Company: Atoa\nhttps://example.com/job") == ["Company: Atoa\nhttps://example.com/job"]
    assert split_posts("   ") == []


def test_text_before_first_header_is_its_own_post():
    posts = split_posts("https://a.example/x\n[10/13, 11:38 PM] Sender: https://b.example/y")
    assert len(posts) == 2


def test_hints_urls_and_emails():
    ibm, nvidia, foxo = (parse_post(p) for p in split_posts(EXPORT))
    assert ibm.hints["company"] == "IBM" and ibm.hints["role"] == "Software Engineering Interns"
    assert ibm.urls == ["https://careers.ibm.com/en_US/careers/JobDetail/Intern-Associate-Engineer/137023"]
    assert nvidia.hints["location"] == "Pune, Bangalore"
    assert foxo.urls == [] and foxo.emails == ["careers@foxo.club"]
    assert foxo.hints["company"] == "FOXO"


# ─── company guesses ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://ag.wd3.myworkdayjobs.com/airbus/job/bangalore/x", "Airbus"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x", "Nvidia"),
    ("https://job-boards.greenhouse.io/cloudsek/jobs/6222340004", "Cloudsek"),
    ("https://gokwik.keka.com/careers/jobdetails/164038", "Gokwik"),
    ("https://terralogic.com/careers/software-engineering-intern", "Terralogic"),
    ("https://docs.google.com/forms/d/e/abc/viewform", None),
])
def test_company_from_url(url, expected):
    assert company_from_url(url) == expected


def test_company_from_email():
    assert company_from_email("mailto:careers@foxo.club") == "Foxo"
    assert company_from_email("mailto:someone@gmail.com") is None


# ─── fetcher ──────────────────────────────────────────────────────────────────

def test_html_to_text_drops_scripts_and_repeats():
    title, text = html_to_text("<html><title>Intern @ Acme</title><script>x()</script><body><p>Build things</p><p>Build things</p></body></html>")
    assert title == "Intern @ Acme" and "x()" not in text and text.count("Build things") == 1


@pytest.mark.parametrize("status,text,reason", [
    (404, "Not found", "http_404"),
    (200, "Sorry, this job is no longer available.", "closed"),
    (200, "Sign in to view this job", "login_wall"),
    (200, "", "empty_page"),
    (200, "Access Google Forms with a personal Google account or Google Workspace account (for business use). " * 6, "login_wall"),
])
def test_unreadable_pages(status, text, reason):
    page = _classify(Page(url="https://x.example", ok=False, status=status, text=text, title="Google Forms: Sign-in" if "Google Forms" in text else ""))
    assert not page.ok and page.reason == reason


def test_normal_page_is_ok():
    assert _classify(Page(url="https://x.example", ok=False, status=200, text="Software Engineering Intern at Acme. " * 20)).ok


# ─── extractor ────────────────────────────────────────────────────────────────

GOOD_PAGE = Page(url="https://careers.ibm.com/x", ok=True, status=200, text="IBM is hiring interns. " * 30)


def test_one_item_per_role_and_all_locations_in_address():
    post = parse_post("Company: RevRag Ai\nLocation: Bangalore\nhttps://x.example/apply")
    llm = fake_llm({
        "company": "RevRag AI",
        "roles": [{"name": "Frontend Intern", "summary": "UI work"}, {"name": "Backend Intern"}, {"name": "AI Engineer Intern"}],
        "locations": [{"city": "Bangalore", "state": "Karnataka", "country": "India"}, {"city": "Pune"}],
        "stipend": "Rs 35000/month", "tags": ["PPO"],
    })
    items, err = extract_internships(post, "https://x.example/apply", GOOD_PAGE, llm)
    assert err is None and [i["name"] for i in items] == ["Frontend Intern", "Backend Intern", "AI Engineer Intern"]
    assert {i["company"] for i in items} == {"RevRag AI"}              # page/LLM company wins over the message line
    assert items[0]["address"] == [
        {"city": "Bangalore", "state": "Karnataka", "country": "India"},
        {"city": "Pune", "state": None, "country": None},
    ]
    assert items[0]["stipend"] == "Rs 35000/month" and items[0]["tags"] == ["PPO"]
    assert items[1]["summary"]                                          # falls back to the page text


def test_company_falls_back_to_message_then_url():
    post = parse_post("Company: Atoa\nRole: Product Interns\nhttps://docs.google.com/forms/d/e/x/viewform")
    items, err = extract_internships(post, "https://docs.google.com/forms/d/e/x/viewform", GOOD_PAGE, fake_llm({"company": None, "roles": []}))
    assert err is None and items[0]["company"] == "Atoa" and items[0]["name"] == "Product Interns"

    post = parse_post("Role: Intern\nhttps://job-boards.greenhouse.io/cloudsek/jobs/1")
    items, err = extract_internships(post, "https://job-boards.greenhouse.io/cloudsek/jobs/1", GOOD_PAGE, fake_llm({"roles": []}))
    assert items[0]["company"] == "Cloudsek"


def test_no_company_is_rejected_with_a_reason():
    post = parse_post("Role: Intern\nhttps://docs.google.com/forms/d/e/x/viewform")
    items, err = extract_internships(post, "https://docs.google.com/forms/d/e/x/viewform", GOOD_PAGE, fake_llm({"roles": []}))
    assert items == [] and "company" in err


def test_location_line_is_split_and_remote_detected():
    post = parse_post("Role: Software Engineering Intern\nLocation: Remote or Bangalore\nCompany: Acme\nhttps://acme.example/j")
    items, err = extract_internships(post, "https://acme.example/j", GOOD_PAGE, fake_llm({"roles": [{"name": "SWE Intern"}]}))
    assert items[0]["address"] == [{"city": "Bangalore", "state": None, "country": None}] and items[0]["isRemote"] is True


def test_email_only_post_uses_mailto_and_email_domain_as_company():
    post = parse_post("Role: AI Intern\nSend your resume to careers@foxo.club")
    items, err = extract_internships(post, "mailto:careers@foxo.club", None, fake_llm({"roles": [{"name": "AI Intern"}]}))
    assert err is None and items[0]["company"] == "Foxo" and items[0]["applyLink"] == "mailto:careers@foxo.club"
    assert "linkVerification" not in items[0]


def test_malformed_json_is_retried_once():
    replies = iter(['{"company": "Acme", "roles": [{"name": "X"}',      # truncated
                    '{"company": "Acme", "roles": [{"name": "SWE Intern"}]}'])
    items, err = extract_internships(parse_post("https://a.example"), "https://a.example", GOOD_PAGE, lambda p: next(replies))
    assert err is None and items[0]["name"] == "SWE Intern"


def test_bad_model_reply_is_a_clean_error():
    items, err = extract_internships(parse_post("x https://a.example"), "https://a.example", GOOD_PAGE, lambda p: "sorry, no")
    assert items == [] and err.startswith("extraction failed")


# ─── pipeline hooks ───────────────────────────────────────────────────────────

def _prepare(item, source):
    import pipeline
    stats = {"saved": 0, "duplicate": 0, "rejected": 0, "errors": 0}
    detail, outcomes = pipeline._new_detail(), {}
    return pipeline._prepare_candidates([item], source, None, stats, detail, outcomes), stats


BASE = {"name": "AI Intern", "company": "Foxo", "summary": "Build things", "address": [{"city": "Bangalore", "state": None, "country": "india"}]}


def test_mailto_is_accepted_only_for_telegram():
    cands, _ = _prepare({**BASE, "applyLink": "mailto:careers@foxo.club"}, "telegram")
    assert cands and cands[0]["applyLink"] == "mailto:careers@foxo.club" and cands[0]["address"][0]["city"] == "Bangalore"
    cands, stats = _prepare({**BASE, "applyLink": "mailto:careers@foxo.club"}, "indeed")
    assert not cands and stats["rejected"] == 1


def test_fingerprint_uses_first_address_city():
    import pipeline
    cands, _ = _prepare({**BASE, "applyLink": "https://a.example/j", "address": [{"city": "Pune"}, {"city": "Bangalore"}]}, "telegram")
    assert cands[0]["fingerprint"] == pipeline.generate_fingerprint("Foxo", "AI Intern", "Pune")


def test_scoring_sees_the_email_domain_and_free_mail_is_never_auto_approved():
    import pipeline
    cands, _ = _prepare({**BASE, "applyLink": "mailto:someone@gmail.com"}, "telegram")
    originals = pipeline._hide_mailto(cands)
    assert cands[0]["applyLink"] == "https://gmail.com"
    pipeline._restore_mailto(cands, originals)
    doc = {**cands[0], "scam_score": 5.0, "decision": "clear", "confidence": 0.9}
    meta = pipeline._assemble_moderation(doc, "telegram")
    assert doc["applyLink"] == "mailto:someone@gmail.com"
    assert meta["status"] == "pending_review" and "free_email_domain" in doc["moderation"]["flags"]
    assert doc["moderation"]["source"] == "community_bot"
    assert "free_email_domain" in doc["moderation"]["scamDetails"]["scamFlags"]
    assert "city" not in doc and "_freeMail" not in doc


def test_work_email_can_be_auto_approved():
    import pipeline
    cands, _ = _prepare({**BASE, "applyLink": "mailto:careers@foxo.club"}, "telegram")
    originals = pipeline._hide_mailto(cands)
    pipeline._restore_mailto(cands, originals)
    meta = pipeline._assemble_moderation({**cands[0], "scam_score": 5.0, "decision": "clear", "confidence": 0.9}, "telegram")
    assert meta["status"] == "auto_approved"


# ─── bot ──────────────────────────────────────────────────────────────────────

def ok_page(url):
    return Page(url=url, ok=True, status=200, text="Intern role at IBM. " * 30)


def test_process_post_saves_each_role_and_reports_the_outcome():
    pushed = []

    def push(item):
        pushed.append(item)
        return "saved", "auto_approved", 12.0

    llm = fake_llm({"company": "IBM", "roles": [{"name": "SWE Intern"}, {"name": "ML Intern"}]})
    lines, approved = bot.process_post("Role: Intern\nhttps://careers.ibm.com/x", fetch=ok_page, llm=llm, push=push)
    assert approved and len(pushed) == 2 and len(lines) == 2
    assert lines[0] == '✅ Saved, auto-approved (scam risk 12/100): "SWE Intern" at IBM'


def test_unreadable_page_means_not_saved():
    pushed = []
    bad = lambda url: Page(url=url, ok=False, reason="login_wall", status=200)
    lines, approved = bot.process_post("https://www.linkedin.com/jobs/view/1", fetch=bad, llm=fake_llm({}), push=pushed.append)
    assert not pushed and not approved and "needs a login" in lines[0]


def test_post_without_link_or_email_is_rejected():
    lines, _ = bot.process_post("Company: Acme\nRole: Intern", fetch=ok_page, llm=fake_llm({}), push=lambda i: ("saved", None, None))
    assert "no apply link or email" in lines[0]


def test_email_post_skips_the_page_fetch():
    fetched, pushed = [], []
    llm = fake_llm({"roles": [{"name": "AI Intern"}]})
    lines, _ = bot.process_post(
        "Company: FOXO\nRole: AI Intern\nEmail your resume to careers@foxo.club",
        fetch=lambda u: fetched.append(u), llm=llm, push=lambda i: (pushed.append(i), ("saved", "pending_review", 40.0))[1],
    )
    assert not fetched and pushed[0]["applyLink"] == "mailto:careers@foxo.club"
    assert lines[0].startswith("⏳ Saved, waiting for a moderator (scam risk 40/100)")


def test_duplicate_is_reported():
    llm = fake_llm({"company": "IBM", "roles": [{"name": "SWE Intern"}]})
    lines, approved = bot.process_post("https://careers.ibm.com/x", fetch=ok_page, llm=llm, push=lambda i: ("duplicate", None, None))
    assert lines == ['↩️ Duplicate: "SWE Intern" at IBM'] and not approved


# ─── webhook ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    bot._seen_updates.clear()
    started = []
    monkeypatch.setattr(bot, "process_message", lambda chat, text: started.append((chat, text)))
    monkeypatch.setattr(bot, "send_message", lambda chat, text: started.append(("sent", text)))
    monkeypatch.setattr(bot.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    app = FastAPI()
    app.include_router(bot.router)
    c = TestClient(app)
    c.started = started
    return c


def update(uid, user=111, text="https://a.example/j"):
    return {"update_id": uid, "message": {"from": {"id": user}, "chat": {"id": user}, "text": text}}


def test_webhook_rejects_wrong_or_missing_secret(client):
    assert client.post("/telegram/webhook", json=update(1)).status_code == 401
    assert client.post("/telegram/webhook", json=update(1), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 401


def test_webhook_ignores_users_not_on_the_allowlist(client):
    h = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
    assert client.post("/telegram/webhook", json=update(2, user=999), headers=h).json() == {"ok": True}
    assert client.started == []


def test_webhook_starts_work_once_per_update(client):
    h = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
    client.post("/telegram/webhook", json=update(3), headers=h)
    client.post("/telegram/webhook", json=update(3), headers=h)          # Telegram retry
    assert [s for s in client.started if s[0] != "sent"] == [(111, "https://a.example/j")]


def test_not_configured_is_503(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    assert client.post("/telegram/webhook", json=update(4)).status_code == 503


# ─── daily AI budget ─────────────────────────────────────────────────────────

def test_post_is_not_processed_once_the_daily_ai_budget_is_used_up(monkeypatch):
    monkeypatch.setattr(bot.usage_log, "budget_exceeded", lambda: True)

    def must_not_fetch(url):
        raise AssertionError("opened a page although the budget is used up")

    lines, approved = bot.process_post("Company: Acme\nRole: Intern\nhttps://acme.com/jobs/1", fetch=must_not_fetch)
    assert approved is False
    assert len(lines) == 1 and "budget" in lines[0] and "midnight IST" in lines[0]


def test_extraction_reports_a_used_up_budget_in_plain_words():
    def over_budget(prompt):
        raise bot.usage_log.BudgetExceeded("daily AI budget of $3.00 reached")

    items, error = extract_internships(parse_post("Company: Acme\nRole: Intern\nhttps://acme.com/jobs/1"), "https://acme.com/jobs/1", None, over_budget)
    assert items == [] and "budget" in error and "midnight IST" in error


def test_calls_are_tagged_with_the_telegram_user(monkeypatch):
    seen = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text: None)
    monkeypatch.setattr(bot, "process_message", lambda chat_id, text: seen.append(bot.usage_log.actor.get()))
    bot._run(1, "hello", 4242)
    assert seen == ["4242"]
