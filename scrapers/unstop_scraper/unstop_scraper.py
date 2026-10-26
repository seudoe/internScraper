"""
Unstop.com Internship Scraper
==============================
Uses Unstop's public REST API to fetch internship data in bulk.

API endpoint:
  GET https://unstop.com/api/public/opportunity/search-result
  ?opportunity=internship&per_page=20&page=N

Returns structured JSON — no Selenium needed for bulk data.
Selenium is used ONLY as a fallback for fields missing from the API.

Strategy:
  - API is the PRIMARY source (structured, fast, reliable)
  - Each page returns up to 20 internships
  - 1000 pages available → up to 20,000 internships
  - We stop after TARGET entries are collected

Install:
    pip install selenium webdriver-manager requests
"""

import re
import json
import time
import logging
import requests
from typing import Optional
from collections import Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("unstop")

# ── Config ────────────────────────────────────────────────────────────────────
TARGET       = 300        # Stop after collecting this many unique internships
PER_PAGE     = 20         # Items per API page (max 20)
MAX_PAGES    = 100        # Safety cap on pages to fetch
OUTPUT_RAW   = "unstop_raw.json"
OUTPUT_FINAL = "unstop_internships.json"

API_BASE = "https://unstop.com/api/public/opportunity/search-result"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://unstop.com/internships",
}

# ── Skills ────────────────────────────────────────────────────────────────────
SKILLS_LIST = [
    "python", "java", "c++", "javascript", "react", "node", "html", "css",
    "sql", "excel", "machine learning", "data analysis", "figma", "aws",
    "azure", "docker", "git", "angular", "vue", "django", "flask", "mongodb",
    "mysql", "postgresql", "typescript", "kotlin", "swift", "flutter",
    "android", "ios", "linux", "bash", "tableau", "power bi", "photoshop",
    "illustrator", "canva", "seo", "google analytics", "wordpress",
    "tensorflow", "pytorch", "pandas", "numpy", "r programming",
]

# ── Role map ──────────────────────────────────────────────────────────────────
ROLE_MAP = {
    "Software Development":   ["software", "developer", "engineer", "backend",
                                "frontend", "full stack", "web", "mobile",
                                "coding", "programming", "app development"],
    "Data Science / AI":      ["data science", "machine learning", "ai",
                                "deep learning", "nlp", "analytics",
                                "data analysis", "data research", "data engineer"],
    "DevOps / Cloud":         ["devops", "cloud", "aws", "azure", "kubernetes",
                                "docker", "infrastructure", "embedded", "rf",
                                "communications", "iot", "firmware"],
    "Design":                 ["design", "ui", "ux", "figma", "graphic",
                                "visual", "creative", "illustration", "motion"],
    "Marketing":              ["marketing", "seo", "digital marketing", "content",
                                "social media", "influencer", "performance marketing",
                                "brand", "campaign", "growth", "pr"],
    "Sales / BD":             ["sales", "business development", "business marketing",
                                "account", "client", "admissions", "crm"],
    "HR":                     ["hr", "human resources", "recruitment", "talent",
                                "talent acquisition", "people operations"],
    "Finance":                ["finance", "accounting", "audit", "tax",
                                "fintech", "investment", "banking"],
    "Cyber Security":         ["cyber", "security", "cybersecurity",
                                "ethical hacking", "penetration", "network security"],
    "Campus Ambassador":      ["campus ambassador", "campus", "ambassador"],
    "Content Writing":        ["content writing", "writing", "blog",
                                "copywriting", "editorial", "journalism"],
    "Operations / Logistics": ["operations", "logistics", "supply chain",
                                "field", "representative", "procurement"],
    "Research":               ["research", "analyst", "market research",
                                "policy", "academic"],
}


# ============================================================================
#  HELPERS
# ============================================================================
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


def clean(s: str) -> str:
    """Strip and collapse whitespace."""
    return re.sub(r"\s+", " ", (s or "").strip())


# ============================================================================
#  API FETCHER
# ============================================================================
def fetch_page(page: int, retries: int = 3) -> Optional[dict]:
    """Fetch one page from the Unstop public API."""
    params = {
        "opportunity": "internships",   # plural → returns internship subtype items
        "per_page":    PER_PAGE,
        "page":        page,
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                API_BASE, params=params, headers=HEADERS, timeout=15
            )
            if resp.status_code == 200:
                return resp.json()
            log.warning("Page %d: HTTP %d (attempt %d)", page, resp.status_code, attempt)
        except requests.RequestException as e:
            log.warning("Page %d: request error %s (attempt %d)", page, e, attempt)
        time.sleep(2 * attempt)
    return None


# ============================================================================
#  PARSE ONE INTERNSHIP FROM API RESPONSE
# ============================================================================
def parse_api_item(item: dict) -> Optional[dict]:
    """
    Extract all fields from a single API result item.

    Key API fields:
      item["title"]                          → title
      item["organisation"]["name"]           → company
      item["seo_url"]                        → link
      item["jobDetail"]["locations"]         → location
      item["jobDetail"]["type"]              → work type (in_office/wfh/hybrid)
      item["jobDetail"]["min_salary"]        → stipend min
      item["jobDetail"]["max_salary"]        → stipend max
      item["jobDetail"]["pay_in"]            → monthly/lump_sum
      item["regnRequirements"]["remain_days"]→ deadline
      item["approved_date"]                  → posted date
      item["details"]                        → description (HTML)
      item["required_skills"]                → skills list
      item["workfunction"]                   → role hints
      item["id"]                             → unique ID
    """
    try:
        # ── Must be an internship (subtype = "internships") ───────────────
        subtype = item.get("subtype", "") or ""
        if "internship" not in subtype.lower():
            return None

        opp_id = str(item.get("id", ""))
        if not opp_id:
            return None

        # ── Title ─────────────────────────────────────────────────────────
        title = clean(item.get("title", ""))
        if not title:
            return None

        # ── Company ───────────────────────────────────────────────────────
        org = item.get("organisation") or {}
        company = clean(org.get("name", "N/A")) or "N/A"

        # ── Link ──────────────────────────────────────────────────────────
        link = item.get("seo_url", "") or ""
        if not link:
            pub = item.get("public_url", "")
            link = f"https://unstop.com/{pub}" if pub else "N/A"

        # ── Job detail (structured stipend + location + work type) ────────
        jd = item.get("jobDetail") or {}

        # ── Location ──────────────────────────────────────────────────────
        location = "N/A"
        jd_type = jd.get("type", "") or ""

        if jd_type in ("wfh", "work_from_home") or "wfh" in jd_type.lower():
            location = "Work From Home"
        elif jd_type == "online":
            location = "Online / Remote"
        else:
            # Try jobDetail.locations list first
            jd_locs = jd.get("locations", []) or []
            if jd_locs:
                location = ", ".join(str(l) for l in jd_locs[:3])
            else:
                # Fallback to item-level locations array
                locs = item.get("locations", []) or []
                cities = [clean(l.get("city", "")) for l in locs if l.get("city")]
                if cities:
                    location = ", ".join(cities[:3])
                else:
                    # Fallback to address
                    addr = item.get("address_with_country_logo") or {}
                    city  = clean(addr.get("city", ""))
                    state = clean(addr.get("state", ""))
                    if city:
                        location = f"{city}, {state}" if state else city
                    elif state:
                        location = state

        # ── Work type ─────────────────────────────────────────────────────
        timing = jd.get("timing", "") or ""
        work_type_map = {
            "full_time":   "Full Time",
            "part_time":   "Part Time",
            "contractual": "Contractual",
            "freelance":   "Freelance",
        }
        work_type = work_type_map.get(timing.lower(), "N/A")

        # ── Stipend ───────────────────────────────────────────────────────
        stipend = "N/A"
        not_disclosed = jd.get("not_disclosed", False)
        paid_unpaid   = jd.get("paid_unpaid", "") or ""
        min_sal = jd.get("min_salary")
        max_sal = jd.get("max_salary")
        pay_in  = jd.get("pay_in", "") or ""
        show_sal = jd.get("show_salary", 0)

        if not_disclosed or paid_unpaid == "unpaid":
            stipend = "Unpaid" if paid_unpaid == "unpaid" else "Not Disclosed"
        elif show_sal and min_sal is not None:
            pay_suffix = "/Month" if "month" in pay_in.lower() else (" Lump Sum" if "lump" in pay_in.lower() else "")
            if max_sal and max_sal != min_sal:
                stipend = f"₹ {int(min_sal):,} - {int(max_sal):,}{pay_suffix}"
            else:
                stipend = f"₹ {int(min_sal):,}{pay_suffix}"

        # ── Deadline ──────────────────────────────────────────────────────
        deadline = "N/A"
        regn = item.get("regnRequirements") or {}
        remain = regn.get("remain_days", "") or ""
        end_dt = regn.get("end_regn_dt")
        if isinstance(end_dt, str) and end_dt.strip():
            deadline = end_dt.strip()   # exact ISO registration end (also for listings that already ended)
        elif remain:
            deadline = clean(remain)
        else:
            arr = regn.get("remainingDaysArray") or {}
            if isinstance(arr, dict):
                dur  = arr.get("durations", "")
                text = arr.get("text", "")
                if dur and text:
                    deadline = f"{dur}{text}".strip()

        # ── Posted date ───────────────────────────────────────────────────
        posted = "N/A"
        approved = item.get("approved_date", "") or ""
        if approved:
            dm = re.match(r"(\d{4})-(\d{2})-(\d{2})", approved)
            if dm:
                months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                y, mo, d = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
                posted = f"{months[mo]} {d}, {y}"

        # ── Description (strip HTML) ───────────────────────────────────────
        description = "N/A"
        details_html = item.get("details", "") or ""
        details_text = re.sub(r"<[^>]+>", " ", details_html)
        details_text = re.sub(r"&[a-z]+;", " ", details_text)
        details_text = re.sub(r"\s+", " ", details_text).strip()
        if details_text:
            sentences = re.split(r"(?<=[.!?])\s+", details_text)
            for s in sentences:
                s = s.strip()
                if len(s) > 30:
                    description = s[:300]
                    break

        # ── Skills from API ───────────────────────────────────────────────
        api_skills = []
        for sk in item.get("required_skills", []):
            name = clean(sk.get("skill_name", "") or sk.get("skill", ""))
            if name:
                api_skills.append(name)

        text_skills = extract_skills(f"{title} {description} {details_text}")
        all_skills = list(dict.fromkeys(api_skills + text_skills))

        # ── Role classification ───────────────────────────────────────────
        wf_names = " ".join(
            wf.get("name", "") for wf in item.get("workfunction", [])
        )
        role = classify_role(title, f"{wf_names} {details_text}")

        return {
            "id":          opp_id,
            "title":       title,
            "company":     company,
            "location":    location,
            "work_type":   work_type,
            "stipend":     stipend,
            "deadline":    deadline,
            "posted":      posted,
            "skills":      all_skills,
            "type":        role,
            "link":        link,
            "description": description,
        }

    except Exception as e:
        log.debug("parse_api_item error: %s", e)
        return None


# ============================================================================
#  DEDUPLICATE
# ============================================================================
def deduplicate(items: list) -> list:
    seen, out = set(), []
    for item in items:
        key = item.get("id") or item.get("link", "")
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ============================================================================
#  SAVE
# ============================================================================
def save_json(items: list, path: str):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, indent=2, ensure_ascii=False))
        log.info("Saved %d items → %s", len(items), path)
    except Exception as e:
        log.warning("Could not save %s: %s", path, e)


# ============================================================================
#  MAIN
# ============================================================================
def main():
    log.info("Unstop Internship Scraper (API mode)")
    log.info("Target: %d internships | Per page: %d | Max pages: %d",
             TARGET, PER_PAGE, MAX_PAGES)

    all_items: list = []
    seen_ids: set   = set()
    consecutive_empty = 0

    for page in range(1, MAX_PAGES + 1):
        log.info("── Fetching page %d (collected %d so far)…", page, len(all_items))

        data = fetch_page(page)
        if not data:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                log.info("3 consecutive failures — stopping.")
                break
            continue

        consecutive_empty = 0

        # Navigate to the items list
        page_data = data.get("data", {})
        if isinstance(page_data, dict):
            items_list = page_data.get("data", [])
        else:
            items_list = []

        if not items_list:
            log.warning("Page %d: empty items list", page)
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        page_count = 0
        for raw in items_list:
            parsed = parse_api_item(raw)
            if parsed and parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                all_items.append(parsed)
                page_count += 1
                log.info(
                    "  ✓ [%d] %-45s | %-25s | %-15s | %s",
                    len(all_items),
                    parsed["title"][:45],
                    parsed["company"][:25],
                    parsed["stipend"][:15],
                    parsed["location"],
                )

        log.info("Page %d: %d new internships (total %d)", page, page_count, len(all_items))

        # Checkpoint save every 5 pages
        if page % 5 == 0:
            save_json(all_items, OUTPUT_RAW)

        # Stop when target reached
        if len(all_items) >= TARGET:
            log.info("Target of %d reached — stopping.", TARGET)
            break

        time.sleep(0.5)  # polite delay between API calls

    # ── Save raw ──────────────────────────────────────────────────────────────
    save_json(all_items, OUTPUT_RAW)

    # ── Dedup + final save ────────────────────────────────────────────────────
    unique = deduplicate(all_items)
    log.info("After dedup: %d unique internships", len(unique))
    save_json(unique, OUTPUT_FINAL)

    # ── Quality report ────────────────────────────────────────────────────────
    if unique:
        fields = ["company", "location", "stipend", "work_type", "deadline", "posted"]
        log.info("=== DATA COMPLETENESS ===")
        for f in fields:
            pct = sum(1 for i in unique if i.get(f, "N/A") not in ("N/A", "")) / len(unique) * 100
            log.info("  %-15s %.0f%%", f, pct)

        log.info("=== ROLE DISTRIBUTION ===")
        for role, cnt in Counter(i.get("type", "Other") for i in unique).most_common():
            log.info("  %-35s %d", role, cnt)

        log.info("=== TOP SKILLS ===")
        all_skills = [s for i in unique for s in i.get("skills", [])]
        for skill, cnt in Counter(all_skills).most_common(10):
            log.info("  %-25s %d", skill, cnt)

    print(f"\nTotal internships scraped: {len(unique)}")
    return unique


if __name__ == "__main__":
    main()
