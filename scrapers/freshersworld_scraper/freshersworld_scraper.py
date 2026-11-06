"""
Freshersworld.com Internship Scraper
======================================
Scrapes internships from https://www.freshersworld.com/jobs/category/internship-job-vacancies

Strategy:
  - Fast, resilient HTTP requests using the embedded schema.org ItemList JSON-LD
  - URL slug extraction is the PRIMARY data source (title, company, location, id)
  - Eliminates heavy browser dependencies, timeouts, and bot-detection blocks
  - Respects TARGET limit and pagination offsets
"""

import re
import json
import os
import time
import logging
import urllib.request
from pathlib import Path
from collections import Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fw")

# ── Config ────────────────────────────────────────────────────────────────────
TARGET       = 300        # Stop after collecting this many unique internships
PAGE_LIMIT   = 20         # Items per page
OUTPUT_DIR   = Path(__file__).resolve().parents[2] / "data" / "scrapers"
OUTPUT_RAW   = OUTPUT_DIR / "fw_raw.json"
OUTPUT_FINAL = OUTPUT_DIR / "fw_internships.json"

BASE_URL = "https://www.freshersworld.com"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── Skills ────────────────────────────────────────────────────────────────────
SKILLS_LIST = [
    "python", "java", "c++", "javascript", "react", "node", "html", "css",
    "sql", "excel", "machine learning", "data analysis", "figma", "aws",
    "azure", "docker", "git", "angular", "vue", "django", "flask", "mongodb",
    "mysql", "postgresql", "typescript", "kotlin", "swift", "flutter",
    "android", "ios", "linux", "bash", "tableau", "power bi", "photoshop",
    "illustrator", "canva", "seo", "google analytics", "wordpress", "tally",
    "autocad", "solidworks", "matlab",
]

# ── Role map ──────────────────────────────────────────────────────────────────
ROLE_MAP = {
    "Software Development":   ["software", "developer", "engineer", "backend",
                                "frontend", "full stack", "web", "mobile",
                                "coding", "programming", "app", "java", "python"],
    "Data Science / AI":      ["data science", "machine learning", "ai",
                                "deep learning", "nlp", "analytics",
                                "data analysis", "data analyst", "data engineer"],
    "DevOps / Cloud":         ["devops", "cloud", "aws", "azure", "kubernetes",
                                "docker", "infrastructure", "embedded", "iot",
                                "firmware", "network", "hardware"],
    "Design":                 ["design", "ui", "ux", "figma", "graphic",
                                "visual", "creative", "illustration", "video editor",
                                "animation", "autocad"],
    "Marketing":              ["marketing", "seo", "digital marketing", "content",
                                "social media", "influencer", "performance marketing",
                                "brand", "campaign", "growth", "pr"],
    "Sales / BD":             ["sales", "business development", "business",
                                "account", "client", "lead generation", "telecall",
                                "tele sales", "field sales"],
    "HR":                     ["hr", "human resources", "recruitment", "talent",
                                "talent acquisition", "people operations"],
    "Finance":                ["finance", "accounting", "audit", "tax",
                                "fintech", "investment", "banking", "ca", "tally",
                                "accounts"],
    "Content Writing":        ["content writing", "writing", "blog",
                                "copywriting", "editorial", "journalism",
                                "academic content"],
    "Operations / Logistics": ["operations", "logistics", "supply chain",
                                "field", "representative", "procurement",
                                "warehouse"],
    "Research":               ["research", "analyst", "market research",
                                "policy", "academic", "lab"],
}


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


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


def parse_slug(href: str) -> dict:
    """
    Extract title, company, location, and job ID from Freshersworld URL slug.
    """
    out = {"title": "", "company": "", "location": "", "id": ""}
    try:
        clean_href = href.split("?")[0].rstrip("/")
        id_match = re.search(r"-(\d{6,})$", clean_href)
        if not id_match:
            return out
        out["id"] = id_match.group(1)

        slug_match = re.search(r"/jobs/(.+)", clean_href)
        if not slug_match:
            return out
        slug = slug_match.group(1)
        slug = re.sub(r"-\d{6,}$", "", slug)

        if "-jobs-opening-in-" in slug:
            parts = slug.split("-jobs-opening-in-", 1)
            title_slug = parts[0]
            rest = parts[1]

            if "-at-" in rest:
                co_loc = rest.split("-at-", 1)
                company_slug  = co_loc[0]
                location_slug = co_loc[1]
            else:
                company_slug  = rest
                location_slug = ""

            out["title"]    = title_slug.replace("-", " ").title()
            out["company"]  = company_slug.replace("-", " ").title()
            out["location"] = location_slug.replace("-", " ").title()
        else:
            out["title"] = slug.replace("-", " ").title()

    except Exception as e:
        log.debug("parse_slug error %s: %s", href, e)
    return out


def save_json(items: list, path: str | Path):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, indent=2, ensure_ascii=False))
        log.info("Saved %d items → %s", len(items), path)
    except Exception as e:
        log.warning("Could not save %s: %s", path, e)


def main(max_items: int | None = None) -> list:
    target_count = max_items or TARGET
    log.info("Freshersworld Internship Scraper (HTTP/JSON-LD)")
    log.info("Target: %d", target_count)

    all_items: list = []
    seen_ids: set = set()

    city_slugs = ["", "delhi", "mumbai", "bangalore", "hyderabad", "pune", "chennai", "noida", "kolkata", "ahmedabad", "gurgaon"]

    for city in city_slugs:
        offset = 0
        empty_pages = 0

        while True:
            if city:
                base = f"{BASE_URL}/jobs/category/internship-job-vacancies-{city}"
            else:
                base = f"{BASE_URL}/jobs/category/internship-job-vacancies"
            url = f"{base}?&limit={PAGE_LIMIT}&offset={offset}"

            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")
            except Exception as e:
                log.debug("Request error for %s: %s", url, e)
                break

            json_lds = re.findall(r'<script type=["\']application/ld\+json["\']>(.*?)</script>', html, re.DOTALL)
            items_found = 0

            for j in json_lds:
                try:
                    data = json.loads(j)
                    if data.get("@type") == "ItemList":
                        elements = data.get("itemListElement", [])
                        for el in elements:
                            item_url = el.get("url")
                            if not item_url:
                                continue
                            id_match = re.search(r"-(\d{6,7})$", item_url.split("?")[0])
                            if not id_match:
                                continue
                            job_id = id_match.group(1)
                            if job_id in seen_ids:
                                continue
                            seen_ids.add(job_id)
                            items_found += 1

                            parsed = parse_slug(item_url)
                            title = parsed["title"] or el.get("name", "Internship")
                            title = re.sub(r"\bjobs opening in.*$", "", title, flags=re.I).strip()
                            company = parsed["company"] or "Freshersworld"
                            location = parsed["location"] or (city.title() if city else "India")

                            role = classify_role(title, f"{title} {company} {location}")
                            skills = extract_skills(f"{title} {role}")
                            if not skills:
                                skills = ["Communication", "Problem Solving"]

                            item = {
                                "id": job_id,
                                "title": title,
                                "name": title,
                                "company": company,
                                "location": location,
                                "salary": "N/A",
                                "stipend": "Unpaid",
                                "duration": "3 months",
                                "experience": "Fresher",
                                "qualifications": "Graduate",
                                "posted": "Recent",
                                "skills": skills,
                                "type": role,
                                "link": item_url,
                                "summary": f"{title} internship at {company}. Location: {location}. Freshers and students welcome to apply.",
                            }
                            all_items.append(item)

                            if len(all_items) >= target_count:
                                break
                except Exception:
                    pass

            if len(all_items) >= target_count:
                break

            if items_found == 0:
                empty_pages += 1
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0

            offset += PAGE_LIMIT

        if len(all_items) >= target_count:
            break

    save_json(all_items, OUTPUT_RAW)
    save_json(all_items, OUTPUT_FINAL)
    log.info("Total Freshersworld internships scraped: %d", len(all_items))
    return all_items


if __name__ == "__main__":
    main()
