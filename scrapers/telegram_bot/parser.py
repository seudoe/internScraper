"""
parser.py — split a Telegram message into posts and pull out what can be read without a page or an LLM.

A message is either one post, or a pasted WhatsApp chat export where each post starts with a header like
    [10/14, 2:01 PM] +91 96693 69502: Company: FOXO
which is the split point.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List

# "[10/14, 2:01 PM] +91 96693 69502: " / "[14/10/2025, 14:01:05] Name: "
WA_HEADER = re.compile(
    r"^\[\d{1,2}/\d{1,2}(?:/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap][Mm])?\]\s*[^:\n]{1,60}:[ \t]*",
    re.MULTILINE,
)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# "Company: IBM", "💼 Role: AI Intern (2 Openings)", "ROLE ; SOFTWARE ENGINEERING INTERNS", "- Location:Pune"
HINT_RE = re.compile(
    r"^[\W_]*(company|role|roles|position|location|stipend|duration|deadline|eligibility|openings)[ \t]*[:;\-][ \t]*(.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_TRAILING_PUNCT = ".,;:!?)"


@dataclass
class Post:
    text: str
    urls: List[str] = field(default_factory=list)
    emails: List[str] = field(default_factory=list)
    hints: Dict[str, str] = field(default_factory=dict)


def split_posts(message: str) -> List[str]:
    """One entry per post. Text before the first export header (if any) counts as its own post."""
    message = (message or "").strip()
    if not message:
        return []
    headers = list(WA_HEADER.finditer(message))
    if not headers:
        return [message]

    parts = [message[: headers[0].start()]]
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(message)
        parts.append(message[h.end():end])
    return [p.strip() for p in parts if p.strip()]


def find_urls(text: str) -> List[str]:
    seen: List[str] = []
    for u in URL_RE.findall(text):
        u = u.rstrip(_TRAILING_PUNCT)
        if u not in seen:
            seen.append(u)
    return seen


def find_emails(text: str) -> List[str]:
    seen: List[str] = []
    for e in EMAIL_RE.findall(text):
        e = e.rstrip(".").lower()
        if e not in seen:
            seen.append(e)
    return seen


def find_hints(text: str) -> Dict[str, str]:
    """First value for each labelled line ("Company: IBM" -> {"company": "IBM"}); labels are lower-cased."""
    hints: Dict[str, str] = {}
    for label, value in HINT_RE.findall(text):
        value = value.strip(" \t-•*")
        if value:
            hints.setdefault(label.lower(), value)
    return hints


def parse_post(text: str) -> Post:
    return Post(text=text, urls=find_urls(text), emails=find_emails(text), hints=find_hints(text))
