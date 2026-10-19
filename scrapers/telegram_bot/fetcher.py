"""
fetcher.py — load the page behind an apply link and reduce it to plain text.

requests first; Selenium when the page is JS-rendered (Workday, Greenhouse, Keka, Google Forms) or blocks plain
clients. A page counts as unreadable (`ok=False`) when it can't be loaded, is a login wall / error page, or is
closed; the caller does not save the post in that case.
"""

import logging
import re
import time
from urllib.parse import urljoin
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("telegram_bot.fetcher")

MAX_TEXT_CHARS = 12000
MIN_USEFUL_CHARS = 800          # shorter than this and we try a real browser (single-page apps return a bare shell)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BLOCKED_STATUSES = {401, 403, 429, 999}   # 999 is LinkedIn

LOGIN_MARKERS = (
    "sign in to view", "log in to view", "join to view", "please log in", "sign in to continue",
    "authwall", "you need permission", "you need access", "enable javascript to",
    "access google forms with a personal google account",
)
CLOSED_MARKERS = (
    "no longer accepting responses", "no longer accepting applications", "this job is no longer available",
    "job is no longer open", "position has been filled", "this posting has expired", "page not found",
    "the form is closed", "this job has expired",
)


@dataclass
class Page:
    url: str
    ok: bool
    reason: Optional[str] = None     # why it is unreadable: http_404, login_wall, closed, empty_page, fetch_failed
    status: Optional[int] = None
    final_url: Optional[str] = None
    title: str = ""
    text: str = ""
    rendered: bool = False           # True when Selenium was needed
    image: Optional[str] = None      # the page's share image (og:image), absolute


def html_to_text(html: str) -> tuple[str, str]:
    """(title, visible text) of an HTML document, with scripts, styles and navigation chrome removed."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    meta = []
    for key in ("og:title", "og:description", "description"):
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            meta.append(tag["content"].strip())
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    body = soup.get_text("\n")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in body.splitlines()]
    text = "\n".join(dict.fromkeys(ln for ln in lines if ln))   # drop blank and repeated lines, keep order
    if meta:
        text = "\n".join(dict.fromkeys(meta)) + "\n" + text
    return title, text[:MAX_TEXT_CHARS]


def find_image(html: str, base_url: str) -> Optional[str]:
    """The page's share image (og:image, else twitter:image) as an absolute http(s) URL."""
    soup = BeautifulSoup(html or "", "html.parser")
    for attr, key in (("property", "og:image"), ("name", "og:image"), ("name", "twitter:image"), ("property", "twitter:image")):
        tag = soup.find("meta", attrs={attr: key})
        content = (tag.get("content") or "").strip() if tag else ""
        if content:
            url = urljoin(base_url, content)
            if url.startswith(("http://", "https://")) and len(url) < 1000:
                return url
    return None


def _classify(page: Page) -> Page:
    """Mark the page unreadable when it is an error page, a login wall, closed, or empty."""
    low = page.text.lower()
    if page.status and page.status >= 400 and len(page.text) < 300:
        page.ok, page.reason = False, f"http_{page.status}"
    elif any(m in low for m in CLOSED_MARKERS):
        page.ok, page.reason = False, "closed"
    elif (len(page.text) < 600 or "sign-in" in page.title.lower()) and any(m in low for m in LOGIN_MARKERS):
        page.ok, page.reason = False, "login_wall"
    elif len(page.text) < 40:
        page.ok, page.reason = False, "empty_page"
    else:
        page.ok, page.reason = True, None
    return page


def _render_with_selenium(url: str, wait_seconds: float = 4.0) -> tuple[Optional[str], str]:
    """(html, final_url) from a headless Chrome, or (None, url) if the browser can't start or load."""
    driver = None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1400,900"):
            opts.add_argument(arg)
        opts.add_argument(f"user-agent={USER_AGENT}")
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
        driver.get(url)
        time.sleep(wait_seconds)    # let client-side rendering finish
        return driver.page_source, driver.current_url
    except Exception as exc:
        log.warning("Selenium render failed for %s: %s", url, str(exc)[:200])
        return None, url
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def fetch_page(url: str, session: Optional[requests.Session] = None) -> Page:
    page = Page(url=url, ok=False, reason="fetch_failed")
    http = session or requests
    try:
        resp = http.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=20, allow_redirects=True)
        page.status, page.final_url = resp.status_code, resp.url
        page.title, page.text = html_to_text(resp.text)
        page.image = find_image(resp.text, resp.url)
    except requests.RequestException as exc:
        log.info("requests failed for %s: %s", url, exc)

    needs_browser = page.status is None or page.status in BLOCKED_STATUSES or len(page.text) < MIN_USEFUL_CHARS
    if needs_browser:
        html, final_url = _render_with_selenium(url)
        if html:
            title, text = html_to_text(html)
            if len(text) > len(page.text):
                page.title, page.text, page.final_url, page.rendered = title, text, final_url, True
                page.image = find_image(html, final_url) or page.image
                if page.status in BLOCKED_STATUSES:
                    page.status = 200     # the browser got through

    if not page.text and page.status is None:
        page.reason = "fetch_failed"
        return page
    return _classify(page)
