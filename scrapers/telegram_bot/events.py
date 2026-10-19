"""
events.py — hackathons and contests sent to the bot as "EVENT ..." messages.

An event is not an internship: it skips the scam pipeline and the moderation queue and goes straight into the `events`
collection that the ifind app reads. The whole message is one event. Its first link is opened, and the LLM reads the page and
the message into the event schema below. The page wins where the two disagree.

An event stays visible until its registration deadline (else its end date, else its start date) has passed, counting that
day in full, IST. With no date at all it is shown for NO_DATE_DAYS after it was added. ifind filters on `expiresAt`; a TTL
index removes the document a few weeks after that.
"""

import logging
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse

from usage_log import BudgetExceeded, IST

from .extractor import PAGE_CHARS, _clean_list, _cohere_llm, _parse_json
from .fetcher import Page
from .parser import Post

log = logging.getLogger("telegram_bot.events")

SOURCE = "telegram"
COLLECTION = "events"
NO_DATE_DAYS = 30
KEEP_AFTER_EXPIRY_DAYS = 21          # TTL: how long an expired event stays in the database
TYPES = ("hackathon", "contest", "challenge", "other")
MODES = ("online", "offline", "hybrid")
HIRING_WORDS = re.compile(r"\b(hiring|hire|intern(ship)?s?|ppo|pre-?placement|job offer|interview|recruit\w*|full[- ]time)\b", re.I)
# page problems that mean the event itself is gone; any other problem just means we work from the message alone
DEAD_PAGE = ("closed", "http_404", "http_410")

PROMPT = """You extract details of a hackathon, coding contest or challenge for a student event board. Reply with ONE JSON object and nothing else.

You get TODAY's date, the LINKS in the message, the PAGE text behind the first link (may be empty) and the MESSAGE someone forwarded.
Rules:
- PAGE is the source of truth. Use MESSAGE only for facts the PAGE does not state. If they conflict, use PAGE.
- Never invent facts. Use null (or []) when something is not stated.
- title: the event's name, without the organiser's tagline.
- organiser: the company or body running it (e.g. Paytm, TCS, Flipkart). A platform that merely hosts it (Unstop, Devpost) is not the organiser.
- type: "hackathon", "contest" (competitive programming and similar), "challenge", or "other".
- mode: "online", "offline" or "hybrid"; location: the city or venue for offline events.
- Dates are YYYY-MM-DD and only when a calendar date is given or can be worked out from TODAY (e.g. "in 50 days").
  start_date and end_date are when the event runs (a one-day event has the same date for both).
  registration_deadline is the last day to register or submit; "Deadline:" lines in the MESSAGE are about this.
- registration_link: the link where a student registers, taken from LINKS or the PAGE.
- summary: 40-80 words on what the event is and what participants do.
- perks: short strings for what participants can get (e.g. "Hiring opportunity with Paytm", "Internship for winners", "Cash prizes"). Only what is stated.
- hiring_opportunity: true only if hiring, an internship, a job or a PPO is offered to participants.
- eligibility: who can participate, as written, or null.
- note: other details worth a student's attention that fit nowhere above (team size, format, rules, rounds, prize amounts, referral codes, fees), at most 400 characters, or null.

JSON shape:
{{"title": str|null, "organiser": str|null, "type": str, "mode": str|null, "location": str|null,
  "start_date": str|null, "end_date": str|null, "registration_deadline": str|null,
  "registration_link": str|null, "summary": str|null, "perks": [str], "hiring_opportunity": bool,
  "eligibility": str|null, "note": str|null}}

TODAY: {today}

LINKS:
{links}

PAGE:
{page}

MESSAGE:
{message}
"""


# ─── Recognising and reading the message ──────────────────────────────────────

_PREFIX = re.compile(r"\s*(event)\b[ \t]*([:\-–—])?[ \t]*", re.IGNORECASE)


def strip_event_prefix(text: str) -> Optional[str]:
    """The message without its EVENT prefix, or None when it isn't an event.

    "EVENT ..." counts, as does "Event: ..." and a first line that is just "event" in any case; a plain "Event Management
    Intern at ..." does not, because that is an internship post.
    """
    m = _PREFIX.match(text or "")
    if not m:
        return None
    rest = text[m.end():]
    first_line_empty = (rest[:1] in ("\n", "\r", "")) and not m.group(2)
    if m.group(1) == "EVENT" or m.group(2) == ":" or first_line_empty:
        return rest.strip()
    return None


def is_event_message(text: str) -> bool:
    return strip_event_prefix(text) is not None


def _day(value) -> Optional[str]:
    """A YYYY-MM-DD string if the value is a real calendar date, else None."""
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat() if value else None
    except ValueError:
        return None


def _text(value, limit: int) -> Optional[str]:
    s = re.sub(r"[ \t]+", " ", str(value or "")).strip()
    return s[:limit] or None


# ─── When an event stops being shown ──────────────────────────────────────────

def end_of_day_ist(day: str) -> datetime:
    """The first instant after `day` in IST, as UTC: the event is still on through the whole of that day."""
    d = date.fromisoformat(day) + timedelta(days=1)
    return datetime.combine(d, dtime.min, tzinfo=IST).astimezone(timezone.utc)


def compute_expiry(registration_deadline: Optional[str], end_date: Optional[str], start_date: Optional[str],
                   now: Optional[datetime] = None) -> Tuple[datetime, bool]:
    """(expiresAt, whether it came from a real date). The deadline decides, else the end date, else the start date."""
    now = now or datetime.now(timezone.utc)
    base = registration_deadline or end_date or start_date
    if base:
        return end_of_day_ist(base), True
    return now + timedelta(days=NO_DATE_DAYS), False


# ─── Matching a repeat of the same event ──────────────────────────────────────

_TRACKING = ("utm_", "ref", "fbclid", "gclid", "source")


def link_key(url: str) -> str:
    """The registration link without scheme, www, tracking parameters, fragment or trailing slash."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (p.hostname or "").lower().removeprefix("www.")
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(_TRACKING)]
    return f"{host}{p.path.rstrip('/')}" + (f"?{urlencode(sorted(query))}" if query else "")


def name_key(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# ─── Extraction ───────────────────────────────────────────────────────────────

def _pick_link(data: Dict, urls: List[str]) -> str:
    """The LLM's registration link when it is one of the message's links, else the first link."""
    wanted = link_key(str(data.get("registration_link") or "")) if data.get("registration_link") else ""
    for u in urls:
        if wanted and link_key(u) == wanted:
            return u
    page_link = str(data.get("registration_link") or "").strip()
    if page_link.startswith("http") and len(page_link) < 600:      # a link only the page knew about
        return page_link
    return urls[0]


def extract_event(
    post: Post,
    body: str,
    page: Optional[Page],
    llm: Optional[Callable[[str], str]] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """(event document fields, error). `error` is a short reason the message can't become an event."""
    now = now or datetime.now(timezone.utc)
    try:
        call = llm or _cohere_llm("event_extract")
        page_text = page.text[:PAGE_CHARS] if page is not None and page.ok else "(the page could not be read)"
        prompt = PROMPT.format(
            today=now.astimezone(IST).date().isoformat(),
            links="\n".join(post.urls[:6]),
            page=page_text,
            message=body[:4000],
        )
        try:
            data = _parse_json(call(prompt))
        except ValueError as exc:
            log.info("model reply was not valid JSON (%s); retrying once", exc)
            data = _parse_json(call(prompt + f"\n\nYour previous reply was not valid JSON ({exc}). Reply with only the JSON object."))
    except BudgetExceeded:
        return None, "today's AI budget is used up (resets at midnight IST)"
    except Exception as exc:
        log.warning("event extraction failed: %s", exc)
        return None, f"extraction failed ({type(exc).__name__})"

    title = _text(data.get("title"), 200)
    if not title:
        return None, "couldn't find the event name"

    start, end = _day(data.get("start_date")), _day(data.get("end_date"))
    deadline = _day(data.get("registration_deadline")) or _day(post.hints.get("deadline"))
    expires_at, dates_known = compute_expiry(deadline, end, start, now)

    perks = [p[:160] for p in _clean_list(data.get("perks"))][:8]
    hiring = bool(data.get("hiring_opportunity")) or any(HIRING_WORDS.search(p) for p in perks)
    link = _pick_link(data, post.urls)
    kind = str(data.get("type") or "").strip().lower()
    mode = str(data.get("mode") or "").strip().lower()
    organiser = _text(data.get("organiser"), 120)

    return {
        "title": title,
        "organiser": organiser,
        "type": kind if kind in TYPES else "other",
        "mode": mode if mode in MODES else None,
        "location": _text(data.get("location"), 160),
        "startDate": start,
        "endDate": end,
        "registrationDeadline": deadline,
        "datesKnown": dates_known,
        "expiresAt": expires_at,
        "link": link,
        "summary": _text(data.get("summary"), 900) or (page.text[:400].strip() if page is not None and page.ok else body[:400].strip()) or None,
        "perks": perks,
        "hiring": hiring,
        "eligibility": _text(data.get("eligibility"), 300),
        "note": _text(data.get("note"), 500),
        "imageUrl": page.image if page is not None and page.ok else None,
        "linkKey": link_key(link),
        "titleKey": name_key(title),
        "orgKey": name_key(organiser),
    }, None


# ─── Storing ──────────────────────────────────────────────────────────────────

def ensure_indexes(col) -> None:
    """The same indexes the ifind Event model declares (matching specs, so neither side errors on the other's)."""
    try:
        col.create_index([("expiresAt", 1)], expireAfterSeconds=KEEP_AFTER_EXPIRY_DAYS * 24 * 3600)
        col.create_index([("linkKey", 1)])
        col.create_index([("titleKey", 1), ("orgKey", 1)])
    except Exception as exc:
        log.warning("could not create event indexes (%s)", exc)


_REPLACEABLE = ("title", "organiser", "type", "mode", "location", "startDate", "endDate", "registrationDeadline", "link",
                "summary", "eligibility", "note", "imageUrl", "linkKey", "titleKey", "orgKey")


def save_event(col, event: Dict, submitted_by: Optional[str] = None, now: Optional[datetime] = None) -> Tuple[str, Dict]:
    """Insert the event, or update the stored copy of the same event. Returns ("saved"|"updated", stored document)."""
    now = now or datetime.now(timezone.utc)
    ensure_indexes(col)
    existing = col.find_one({"$or": [
        {"linkKey": event["linkKey"]},
        *([{"titleKey": event["titleKey"], "orgKey": event["orgKey"]}] if event["orgKey"] else []),
    ]})

    if existing is None:
        doc = {**event, "hidden": False, "source": SOURCE, "submittedBy": submitted_by, "createdAt": now, "updatedAt": now}
        col.insert_one(doc)
        return "saved", doc

    changes: Dict = {k: event[k] for k in _REPLACEABLE if event.get(k)}
    if event["perks"] or event["hiring"]:
        changes["perks"] = event["perks"]
        changes["hiring"] = event["hiring"]
    if event["datesKnown"]:                      # a resend without dates must not undo the dates already stored
        changes.update(expiresAt=event["expiresAt"], datesKnown=True)
    changes["updatedAt"] = now
    col.update_one({"_id": existing["_id"]}, {"$set": changes})      # `hidden` stays as a moderator left it
    return "updated", {**existing, **changes}
