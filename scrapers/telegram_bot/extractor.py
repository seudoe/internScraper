"""
extractor.py — turn one forwarded post (+ the page behind its link) into pipeline items.

Output is the raw dict shape pipeline.py `_prepare_candidates` reads (name, company, applyLink, summary,
address[], skills, stipend, duration, deadline_date, ...), one item per role in the post.

Where the page and the message disagree the page wins: it is the company's own listing. The message only
fills what the page doesn't say. Company falls back: page/message (LLM) -> a "Company:" line -> the apply URL
(or the email domain for mailto: posts).
"""

import json
import logging
import os
import re
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from usage_log import BudgetExceeded, tracked_chat

from .fetcher import Page
from .parser import Post

log = logging.getLogger("telegram_bot.extractor")

COHERE_MODEL = "command-a-03-2025"      # same model github_scraper.py uses
PAGE_CHARS = 8000

PROMPT = """You extract internship details for a job board. Reply with ONE JSON object and nothing else.

You get the apply URL, the PAGE text behind it (may be empty) and the MESSAGE someone forwarded.
Rules:
- PAGE is the source of truth. Use MESSAGE only for facts the PAGE does not state. If they conflict, use PAGE.
- Never invent facts. Use null (or []) when something is not stated.
- company: the hiring company's name. It is often only visible in the PAGE text, its title or the URL.
- roles: one entry per distinct internship role. If the post has a single role, one entry.
- stipend: only a concrete figure as written (e.g. "Rs 35000/month"). Phrases like "competitive stipend" -> null.
- tags: short labels such as "PPO", "2026 batch", "mass hiring" when stated.
- deadline: YYYY-MM-DD only if a calendar date is given.

JSON shape:
{{"company": str|null,
  "roles": [{{"name": str, "summary": "50-90 words on what the role involves", "skills": [str], "responsibilities": [str]}}],
  "locations": [{{"city": str|null, "state": str|null, "country": str|null}}],
  "is_remote": bool,
  "stipend": str|null, "duration": str|null, "deadline": str|null,
  "degree": [str]|null, "field": [str]|null, "openings": int|null, "perks": [str]|null, "tags": [str]}}

APPLY URL: {url}

PAGE:
{page}

MESSAGE:
{message}
"""

_LOCALE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")
_GENERIC_LABELS = {"docs", "forms", "www", "jobs", "careers", "career", "apply", "job-boards", "boards", "app"}
_NO_GUESS_HOSTS = ("google.com", "linkedin.com", "forms.gle", "bit.ly")
_ATS_PATH_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com")
_SPLIT_LOCATIONS = re.compile(r"\s*(?:,|/|&|\bor\b|\band\b|\|)\s*", re.IGNORECASE)
_REMOTE_RE = re.compile(r"\b(remote|wfh|work from home)\b", re.IGNORECASE)


def company_from_url(url: str) -> Optional[str]:
    """Last-resort company guess from an ATS / careers URL: greenhouse.io/<co>, <co>.myworkdayjobs.com, <co>.keka.com, <co>.com."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host or host.endswith(_NO_GUESS_HOSTS):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    labels = host.split(".")

    if host.endswith("myworkdayjobs.com"):
        path = [p for p in parts if not _LOCALE.match(p)]
        # the tenant is the company on most boards ("nvidia.wd5..."); a short code ("ag.wd3...") means the name is in the path
        slug = labels[0] if len(labels[0]) > 3 else (path[0] if path else labels[0])
    elif host.endswith(_ATS_PATH_HOSTS):
        slug = parts[0] if parts else ""
    else:
        # the registered name: "terralogic.com" -> terralogic, "gokwik.keka.com" -> gokwik, "careers.ibm.com" -> ibm
        reg = labels[-3] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac") else labels[-2] if len(labels) >= 2 else ""
        if host.endswith(("keka.com", "zohorecruit.com", "freshteam.com", "bamboohr.com", "recruitee.com")):
            reg = labels[0]
        slug = reg

    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug.title() if slug and slug.lower() not in _GENERIC_LABELS else None


def company_from_email(link: str) -> Optional[str]:
    """Company name from a work email's domain (mailto:hr@foxo.club -> Foxo); None for free-mail domains."""
    from pipeline import FREE_MAIL_DOMAINS, mailto_domain

    domain = mailto_domain(link)
    if not domain or domain in FREE_MAIL_DOMAINS:
        return None
    return domain.split(".")[0].replace("-", " ").title()


def _cohere_llm() -> Callable[[str], str]:
    import cohere

    key = os.environ.get("COHERE_API_KEY")
    if not key:
        raise RuntimeError("COHERE_API_KEY is not set")
    co = cohere.Client(key)

    def call(prompt: str) -> str:
        return tracked_chat(co, "telegram", "extract", model=COHERE_MODEL, message=prompt, max_tokens=2500,
                            temperature=0, response_format={"type": "json_object"}).text
    return call


def _parse_json(raw: str) -> Dict:
    text = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start:end + 1])


def _clean_list(v) -> List[str]:
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def _locations(data: Dict, hints: Dict[str, str]) -> Tuple[List[Dict], bool]:
    """(address list, is_remote). LLM locations first, then the message's "Location:" line."""
    is_remote = bool(data.get("is_remote"))
    out: List[Dict] = []
    for loc in data.get("locations") or []:
        if isinstance(loc, dict) and (loc.get("city") or loc.get("state")):
            out.append({k: (str(loc.get(k)).strip() or None) if loc.get(k) else None for k in ("city", "state", "country")})
    if not out and hints.get("location"):
        for part in _SPLIT_LOCATIONS.split(hints["location"]):
            part = part.strip(" .")
            if not part:
                continue
            if _REMOTE_RE.search(part):
                is_remote = True
            else:
                out.append({"city": part, "state": None, "country": None})
    return out, is_remote


def _link_verification(page: Optional[Page]) -> Optional[Dict]:
    if page is None:
        return None
    return {
        "reachable": True,
        "statusCode": page.status or 200,
        "redirectedTo": page.final_url if page.final_url and page.final_url != page.url else None,
        "isScamSuspected": False,
        "isExpired": False,
        "scamSignals": [],
    }


def extract_internships(
    post: Post,
    apply_link: str,
    page: Optional[Page],
    llm: Optional[Callable[[str], str]] = None,
) -> Tuple[List[Dict], Optional[str]]:
    """
    Returns (items, error). `error` is a short human reason ("couldn't find the company", ...) when the post
    can't be turned into a valid internship; `items` is then empty.
    """
    try:
        call = llm or _cohere_llm()
        page_text = page.text[:PAGE_CHARS] if page is not None and page.ok else "(no page: applied for by email)"
        prompt = PROMPT.format(url=apply_link, page=page_text, message=post.text[:4000])
        try:
            data = _parse_json(call(prompt))
        except ValueError as exc:       # malformed JSON (json.JSONDecodeError is a ValueError): ask once more
            log.info("model reply was not valid JSON (%s); retrying once", exc)
            data = _parse_json(call(prompt + f"\n\nYour previous reply was not valid JSON ({exc}). Reply with only the JSON object."))
    except BudgetExceeded:
        return [], "today's AI budget is used up (resets at midnight IST)"
    except Exception as exc:
        log.warning("extraction failed: %s", exc)
        return [], f"extraction failed ({type(exc).__name__})"

    hints = post.hints
    company = (
        str(data.get("company") or "").strip()
        or hints.get("company", "").strip()
        or (company_from_url(apply_link) if apply_link.startswith("http") else company_from_email(apply_link))
    )
    if not company:
        return [], "couldn't find the company"

    roles = [r for r in (data.get("roles") or []) if isinstance(r, dict) and str(r.get("name") or "").strip()]
    if not roles and hints.get("role"):
        roles = [{"name": hints["role"]}]
    if not roles:
        return [], "couldn't find the role"

    address, is_remote = _locations(data, hints)
    stipend = str(data.get("stipend") or "").strip()
    fallback_summary = (page.text[:600] if page is not None and page.ok else post.text).strip()

    items: List[Dict] = []
    for r in roles:
        item = {
            "name": str(r["name"]).strip(),
            "company": company,
            "applyLink": apply_link,
            "summary": str(r.get("summary") or "").strip() or fallback_summary,
            "address": address,
            "isRemote": is_remote,
            "skills": _clean_list(r.get("skills")),
            "responsibilities": _clean_list(r.get("responsibilities")) or None,
            "degree": _clean_list(data.get("degree")) or None,
            "field": _clean_list(data.get("field")) or None,
            "perks": _clean_list(data.get("perks")) or None,
            "tags": _clean_list(data.get("tags")) or None,
            "openings": data.get("openings") if isinstance(data.get("openings"), int) else None,
            "deadline_date": data.get("deadline") or None,
        }
        if not address and is_remote:
            item["location"] = "Remote"
        if stipend:
            item["stipend"] = stipend
        if data.get("duration"):
            item["duration"] = str(data["duration"])
        lv = _link_verification(page)
        if lv:
            item["linkVerification"] = lv
        items.append(item)
    return items, None
