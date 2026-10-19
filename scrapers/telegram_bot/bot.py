"""
bot.py — Telegram webhook: internships forwarded from WhatsApp go through the moderation pipeline.

  Telegram --POST /telegram/webhook--> split into posts -> fetch each apply link -> extract (Cohere)
        -> pipeline.push_to_pipeline(source="telegram") -> reply with the outcome of every post

The webhook answers Telegram at once and does the work in a background thread (fetching pages and calling the
LLM takes far longer than Telegram waits); results arrive as bot messages.

Environment:
  TELEGRAM_BOT_TOKEN          from @BotFather
  TELEGRAM_WEBHOOK_SECRET     any random string; Telegram sends it back in a header and we reject requests without it
  TELEGRAM_ALLOWED_USER_IDS   comma-separated Telegram user ids allowed to submit; everyone else is ignored
  COHERE_API_KEY              extraction (already used by the scrapers)

Register the webhook once (after deploying):
  python -m scrapers.telegram_bot.bot set-webhook https://<your-render-host>/telegram/webhook
"""

import hmac
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests
from fastapi import APIRouter, Body, Header, HTTPException

import usage_log

from . import events
from .extractor import extract_internships
from .fetcher import Page, fetch_page
from .parser import Post, parse_post, split_posts

log = logging.getLogger("telegram_bot")

router = APIRouter()

SOURCE = "telegram"
MAX_POSTS_PER_MESSAGE = 40
REPLY_GAP_SECONDS = 0.6          # Telegram allows ~1 message/second per chat
HELP = (
    "Send me internship posts (a WhatsApp forward, or a pasted chat export with several posts).\n"
    "I open the apply link, read the details from the page and add it to the review queue.\n"
    "A post needs an apply link, or an email address to apply to.\n\n"
    "For a hackathon or contest, start the message with EVENT and add its registration link. "
    "I read the page and list it for students until its deadline passes."
)
PAGE_PROBLEMS = {
    "closed": "the posting looks closed",
    "login_wall": "the page needs a login",
    "empty_page": "the page was empty",
    "fetch_failed": "couldn't reach the page",
}

_seen_updates: deque = deque(maxlen=500)
_work_lock = threading.Lock()    # one browser/LLM job at a time keeps memory bounded on a small instance


# ─── Telegram API ─────────────────────────────────────────────────────────────

def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return token


def send_message(chat_id: int, text: str) -> None:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_token()}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
            timeout=15,
        )
        if not resp.ok:      # e.g. the user never pressed Start, or blocked the bot
            log.warning("sendMessage rejected (%s): %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("sendMessage failed: %s", exc)


def set_webhook(url: str) -> dict:
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is not set")
    resp = requests.post(
        f"https://api.telegram.org/bot{_token()}/setWebhook",
        json={"url": url, "secret_token": secret, "allowed_updates": ["message", "edited_message"]},
        timeout=15,
    )
    return resp.json()


def allowed_user_ids() -> Set[int]:
    ids: Set[int] = set()
    for part in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# ─── One post -> reply lines ──────────────────────────────────────────────────

def _short(url: str, n: int = 60) -> str:
    return url if len(url) <= n else url[: n - 1] + "…"


def _targets(post: Post) -> List[str]:
    """Apply links of a post: every URL in it, else the first email address as a mailto: link."""
    if post.urls:
        return list(post.urls)
    if post.emails:
        return [f"mailto:{post.emails[0]}"]
    return []


def _page_problem(page: Page) -> str:
    if page.reason and page.reason.startswith("http_"):
        return f"the page returned {page.reason[5:]}"
    return PAGE_PROBLEMS.get(page.reason or "", "the page couldn't be read")


def push_item(item: dict) -> Tuple[str, Optional[str], Optional[float]]:
    """
    Run one extracted item through the pipeline. Returns (outcome, moderation status, scam score) where
    outcome is "saved", "duplicate" or "rejected"; raises on infrastructure errors.
    """
    from pipeline import STAGING_COLLECTION, push_to_pipeline
    from script import _open_staging

    client, col = _open_staging()
    try:
        stats = push_to_pipeline([item], SOURCE, col, label=SOURCE)
        if stats["saved"]:
            doc = col.find_one({"source": SOURCE, "applyLink": item["applyLink"], "name": item["name"]},
                               {"moderation": 1}, sort=[("createdAt", -1)])
            mod = (doc or {}).get("moderation") or {}
            return "saved", mod.get("status"), mod.get("score")
        if stats["duplicate"]:
            return "duplicate", None, None
        return "rejected", None, None
    finally:
        client.close()


def _describe(item: dict, outcome: Tuple[str, Optional[str], Optional[float]]) -> str:
    kind, status, score = outcome
    label = f'"{item["name"]}" at {item["company"]}'
    if kind == "duplicate":
        return f"↩️ Duplicate: {label}"
    if kind == "rejected":
        return f"❌ Rejected: {label} (missing details the pipeline needs)"
    shown = f" (scam risk {score:.0f}/100)" if score is not None else ""
    verdict = {
        "auto_approved": "✅ Saved, auto-approved",
        "pending_review": "⏳ Saved, waiting for a moderator",
        "auto_rejected": "⛔ Saved, but auto-rejected as likely scam",
    }.get(status or "", "✅ Saved")
    return f"{verdict}{shown}: {label}"


def process_post(
    text: str,
    fetch: Callable[[str], Page] = fetch_page,
    llm: Optional[Callable[[str], str]] = None,
    push: Callable[[dict], Tuple[str, Optional[str], Optional[float]]] = push_item,
) -> Tuple[List[str], bool]:
    """Handle one post. Returns (reply lines, whether anything was auto-approved)."""
    post = parse_post(text)
    targets = _targets(post)
    if not targets:
        return ["❌ Rejected: no apply link or email address in this post"], False
    if llm is None and usage_log.budget_exceeded():     # don't open pages we can't afford to read
        return ["⏸️ Not processed: today's AI budget is used up. Send it again after midnight IST."], False

    lines: List[str] = []
    approved = False
    for link in targets:
        page: Optional[Page] = None
        if link.startswith("http"):
            page = fetch(link)
            if not page.ok:
                lines.append(f"❌ Rejected: {_page_problem(page)} ({_short(link)})")
                continue

        items, error = extract_internships(post, link, page, llm)
        if error:
            lines.append(f"❌ Rejected: {error} ({_short(link)})")
            continue

        for item in items:
            try:
                outcome = push(item)
            except Exception as exc:
                log.exception("pipeline failed for %s", item.get("name"))
                lines.append(f'❌ Error saving "{item["name"]}": {type(exc).__name__}')
                continue
            approved = approved or outcome[1] == "auto_approved"
            lines.append(_describe(item, outcome))
    return lines, approved


# ─── Events ───────────────────────────────────────────────────────────────────

def push_event(event: dict, submitted_by: Optional[str] = None) -> Tuple[str, dict]:
    """Store an event in the `events` collection. Returns ("saved"|"updated", stored document); raises on infrastructure errors."""
    from script import _open_staging

    client, staging = _open_staging()
    try:
        return events.save_event(staging.database[events.COLLECTION], event, submitted_by)
    finally:
        client.close()


def _on(day: Optional[str]) -> str:
    return datetime.fromisoformat(day).strftime("%d %b %Y").lstrip("0") if day else ""


def _describe_event(kind: str, doc: dict) -> str:
    label = f'"{doc["title"]}"' + (f' by {doc["organiser"]}' if doc.get("organiser") else "")
    verb = "🔄 Updated event" if kind == "updated" else "🎉 Saved event"
    if doc.get("datesKnown"):
        last = doc.get("registrationDeadline") or doc.get("endDate") or doc.get("startDate")
        when = f" · shown until {_on(last)}"
    else:
        when = f" · no dates found, shown for {events.NO_DATE_DAYS} days (resend with a Deadline: line to set one)"
    return f"{verb}: {label}{when}"


def process_event(
    body: str,
    fetch: Callable[[str], Page] = fetch_page,
    llm: Optional[Callable[[str], str]] = None,
    push: Callable[[dict], Tuple[str, dict]] = push_event,
    now: Optional[datetime] = None,
) -> List[str]:
    """Handle one EVENT message (prefix already removed). Returns the reply lines."""
    post = parse_post(body)
    if not post.urls:
        return ["❌ Not saved: no registration link in this message"]
    if llm is None and usage_log.budget_exceeded():
        return ["⏸️ Not processed: today's AI budget is used up. Send it again after midnight IST."]

    page = fetch(post.urls[0])
    if not page.ok:
        if page.reason in events.DEAD_PAGE:
            return [f"❌ Not saved: {_page_problem(page)} ({_short(post.urls[0])})"]
        page = None      # login wall, blocked or unreachable: work from the message alone

    event, error = events.extract_event(post, body, page, llm, now)
    if error:
        return [f"❌ Not saved: {error}"]
    if event["datesKnown"] and event["expiresAt"] <= (now or datetime.now(timezone.utc)):
        return [f'❌ Not saved: "{event["title"]}" has already ended']
    try:
        kind, doc = push(event)
    except Exception as exc:
        log.exception("saving event failed")
        return [f"❌ Error saving the event: {type(exc).__name__}"]
    return [_describe_event(kind, doc)]


def process_message(chat_id: int, text: str) -> None:
    """Background job for one Telegram message: every post gets its own reply."""
    body = events.strip_event_prefix(text)
    if body is not None:
        with _work_lock:
            try:
                lines = process_event(body, push=lambda ev: push_event(ev, usage_log.actor.get()))
            except Exception as exc:
                log.exception("event failed")
                lines = [f"❌ Error: {type(exc).__name__}: {str(exc)[:120]}"]
        for line in lines:
            send_message(chat_id, line)
        return

    posts = split_posts(text)
    if not posts:
        return
    if len(posts) > MAX_POSTS_PER_MESSAGE:
        send_message(chat_id, f"That's {len(posts)} posts; I'll do the first {MAX_POSTS_PER_MESSAGE}. Send the rest again.")
        posts = posts[:MAX_POSTS_PER_MESSAGE]

    any_approved = False
    with _work_lock:
        for n, post_text in enumerate(posts, 1):
            prefix = f"[{n}/{len(posts)}] " if len(posts) > 1 else ""
            try:
                lines, approved = process_post(post_text)
            except Exception as exc:
                log.exception("post failed")
                lines, approved = [f"❌ Error: {type(exc).__name__}: {str(exc)[:120]}"], False
            any_approved = any_approved or approved
            for line in lines:
                send_message(chat_id, prefix + line)
                time.sleep(REPLY_GAP_SECONDS)

    log.info("message done", extra={"f": {"posts": len(posts), "autoApproved": any_approved}})
    if any_approved:
        try:
            from script import _trigger_vectorizer
            _trigger_vectorizer()
        except Exception as exc:
            log.warning("vectoriser trigger failed: %s", exc)


def _run(chat_id: int, text: str, sender: Optional[int] = None) -> None:
    usage_log.actor.set(str(sender) if sender is not None else None)   # tags this thread's LLM calls with the Telegram user
    send_message(chat_id, "Got it, working on it…")   # in the thread so the webhook answers Telegram at once
    process_message(chat_id, text)


# ─── Webhook ──────────────────────────────────────────────────────────────────

def handle_update(update: Dict) -> bool:
    """Validate an update and start the background job. Returns True when work was started."""
    uid = update.get("update_id")
    if uid is not None:
        if uid in _seen_updates:      # Telegram retries when it doesn't get a 200 in time
            return False
        _seen_updates.append(uid)

    message = update.get("message") or update.get("edited_message") or {}
    sender = (message.get("from") or {}).get("id")
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or message.get("caption") or "").strip()
    if sender not in allowed_user_ids() or chat_id is None or not text:
        return False

    if text.split()[0].split("@")[0] in ("/start", "/help"):
        send_message(chat_id, HELP)
        return False

    threading.Thread(target=_run, args=(chat_id, text, sender), daemon=True).start()
    return True


@router.post("/telegram/webhook")
def telegram_webhook(
    update: Dict = Body(...),
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    """Telegram bot webhook: receives forwarded internship posts and runs them through the pipeline"""
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", secret):
        raise HTTPException(status_code=401, detail="Invalid secret token")
    handle_update(update)
    return {"ok": True}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "set-webhook":
        from dotenv import load_dotenv
        load_dotenv()
        print(set_webhook(sys.argv[2]))
    else:
        sys.exit("usage: python -m scrapers.telegram_bot.bot set-webhook https://<host>/telegram/webhook")
