"""
LetsIntern Scraper (letsintern.in)
===================================
Scrapes internships from https://letsintern.in/current-internships/

Strategy:
  1. Load the listing page and collect all internship detail URLs
  2. For each URL, visit the detail page and extract fields from plain HTML text
  3. Uses fast, resilient HTTP requests (replaces slow Selenium and avoids container hang)
  4. Respects TARGET / MAX_ITEMS cap
"""

import re
import json
import os
import time
import logging
import urllib.request
from pathlib import Path
from typing import Optional
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("letsintern")

# ── Config ────────────────────────────────────────────────────────────────────
LISTING_PAGES = [
    "https://letsintern.in/current-internships/",
    "https://letsintern.in/jobs/",
]
TARGET       = 50         # Default cap
MAX_ITEMS    = None       # Set dynamically by caller/script
OUTPUT_DIR   = Path(__file__).resolve().parents[2] / "data" / "scrapers"
OUTPUT_RAW   = OUTPUT_DIR / "letsintern_raw.json"
OUTPUT_FINAL = OUTPUT_DIR / "letsintern_internships.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SKILLS_LIST = [
    "python", "java", "c++", "javascript", "react", "node", "html", "css",
    "sql", "excel", "machine learning", "data analysis", "figma", "aws",
    "azure", "docker", "git", "angular", "vue", "django", "flask", "mongodb",
    "mysql", "postgresql", "typescript", "kotlin", "swift", "flutter",
    "android", "ios", "linux", "bash", "tableau", "power bi", "ai",
    "deep learning", "nlp", "devops", "kubernetes", "tensorflow", "pytorch",
    "opencv", "pandas", "numpy", "scikit-learn", "photoshop", "illustrator",
    "figma", "canva", "wordpress", "php", "laravel", "spring", "hibernate",
]

ROLE_MAP = {
    "Software Development": ["software", "developer", "engineer", "backend", "frontend", "full stack", "web", "mobile", "app"],
    "Data Science / AI":    ["data science", "machine learning", "ai", "deep learning", "nlp", "analytics", "data analyst"],
    "DevOps / Cloud":       ["devops", "cloud", "aws", "azure", "kubernetes", "docker", "infrastructure"],
    "Design":               ["design", "ui", "ux", "figma", "graphic", "creative", "photoshop"],
    "Marketing":            ["marketing", "seo", "digital marketing", "content", "social media", "growth"],
    "Sales / BD":           ["sales", "business development", "account manager"],
    "HR":                   ["hr", "human resources", "recruitment", "talent"],
    "Finance":              ["finance", "accounting", "audit", "tax"],
    "Testing / QA":         ["qa", "quality assurance", "testing", "automation testing"],
    "Content":              ["content", "writing", "blog", "copy", "editor"],
}


def extract_skills(text: str) -> list:
    lower = text.lower()
    return sorted({s.capitalize() for s in SKILLS_LIST
                   if re.search(rf"\b{re.escape(s)}\b", lower)})


def classify_role(title: str, text: str) -> str:
    combined = (title + " " + text).lower()
    scores = {
        cat: sum(3 if kw in title.lower() else 1 for kw in kws if kw in combined)
        for cat, kws in ROLE_MAP.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Other"


def collect_internship_urls(listing_url: str) -> list[str]:
    urls = []
    try:
        req = urllib.request.Request(listing_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("https://letsintern.in/"):
                continue
            path = href.replace("https://letsintern.in/", "").strip("/")
            if not path or "/" in path or "#" in path:
                continue
            if any(skip in path for skip in ["about", "contact", "privacy", "terms", "jobs", "blog", "category", "current-internships"]):
                continue
            final_url = f"https://letsintern.in/{path}/"
            if final_url not in urls:
                urls.append(final_url)
    except Exception as e:
        log.warning("Could not load listing page %s: %s", listing_url, e)
    return urls


def parse_detail_page(url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")

        title = "Internship"
        if soup.title and soup.title.string:
            raw_title = soup.title.string.split("|")[0].split("-")[0].strip()
            title = re.sub(r"\b(202\d|internship program)\b", "", raw_title, flags=re.I).strip()
            if not title:
                title = raw_title

        text = soup.get_text()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        duration = "3 months"
        stipend = "Unpaid"
        for line in lines:
            if re.search(r"duration[:\s]+(\d+\s*(?:month|week)s?)", line, re.I):
                m = re.search(r"duration[:\s]+(\d+\s*(?:month|week)s?)", line, re.I)
                duration = m.group(1)
            if re.search(r"stipend[:\s]+(₹?\s*\d+[\d,]*|performance|unpaid)", line, re.I):
                m = re.search(r"stipend[:\s]+(₹?\s*\d+[\d,]*|performance|unpaid)", line, re.I)
                stipend = m.group(1)

        desc = next((l for l in lines if len(l) > 40 and not any(s in l.lower() for s in ["skip to content", "follow us", "cookie", "whatsapp", "apply now"])), f"{title} internship at LetsIntern.")

        role = classify_role(title, text)
        skills = extract_skills(f"{title} {desc}")
        if not skills:
            skills = ["Communication", "Problem Solving"]

        return {
            "title":       title,
            "name":        title,
            "company":     "LetsIntern",
            "location":    "Remote / WFH",
            "stipend":     stipend,
            "duration":    duration,
            "start_date":  "Immediately",
            "apply_by":    "Open until filled",
            "skills":      skills,
            "type":        role,
            "link":        url,
            "apply_link":  url,
            "summary":     desc,
            "description": desc,
        }
    except Exception as e:
        log.debug("parse_detail_page error for %s: %s", url, e)
        return None


def save_json(items: list, path: str | Path):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, indent=2, ensure_ascii=False))
        log.info("Saved %d items -> %s", len(items), path)
    except Exception as e:
        log.warning("Could not save %s: %s", path, e)


def main(max_items: int | None = None) -> list:
    limit = max_items or MAX_ITEMS or TARGET
    log.info("=== LetsIntern Scraper (HTTP/BS4) ===")
    log.info("Cap: %d", limit)

    urls = []
    for lpage in LISTING_PAGES:
        purls = collect_internship_urls(lpage)
        for u in purls:
            if u not in urls:
                urls.append(u)

    log.info("Total unique internship URLs found: %d", len(urls))
    results = []

    for idx, url in enumerate(urls, 1):
        if len(results) >= limit:
            break
        item = parse_detail_page(url)
        if item:
            results.append(item)
            log.info("  [%d/%d] OK %-45s | %-12s", idx, min(limit, len(urls)), item["title"][:45], item["duration"])
        time.sleep(0.1)

    save_json(results, OUTPUT_RAW)
    save_json(results, OUTPUT_FINAL)
    log.info("Scraped %d internships from %d URLs", len(results), len(urls))
    return results


if __name__ == "__main__":
    main()
