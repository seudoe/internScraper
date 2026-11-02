"""
Freshersworld.com Internship Scraper
======================================
Scrapes internships from https://www.freshersworld.com/jobs/category/internship-job-vacancies

Strategy:
  - URL slug is the PRIMARY data source (always available, never empty)
    e.g. /jobs/data-analyst-internship-jobs-opening-in-nuage-compusys-at-bangalore-2874625
    → title: Data Analyst Internship, company: Nuage Compusys, location: Bangalore, id: 2874625
  - Card text is SECONDARY (enriches with salary, experience, qualifications, posted date)
  - Scrapes the main category page + city-specific pages with offset pagination
  - Deduplicates by numeric job ID extracted from URL

Install:
    pip install selenium webdriver-manager
"""

import re
import json
import os
from pathlib import Path
import time
import logging
from typing import Optional
from collections import Counter

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

try:
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.core.driver_cache import DriverCacheManager
    USE_WDM = True
except ImportError:
    USE_WDM = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fw")

# ── Config ────────────────────────────────────────────────────────────────────
TARGET       = 300        # Stop after collecting this many unique internships
PAGE_LIMIT   = 20         # Items per page (Freshersworld default)
PAGE_WAIT    = 8          # Seconds to wait for page load
OUTPUT_DIR   = Path(__file__).resolve().parents[2] / "data" / "scrapers"   # <project>/data/scrapers
OUTPUT_RAW   = OUTPUT_DIR / "fw_raw.json"
OUTPUT_FINAL = OUTPUT_DIR / "fw_internships.json"

BASE_URL = "https://www.freshersworld.com"

# City-specific pages to scrape (each has its own independent listing set)
CITY_SLUGS = [
    "",           # main page (all India)
    "delhi",
    "mumbai",
    "bangalore",
    "hyderabad",
    "pune",
    "chennai",
    "noida",
    "kolkata",
    "ahmedabad",
    "gurgaon",
]

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


# ============================================================================
#  DRIVER
# ============================================================================
def setup_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    if USE_WDM:
        svc = Service(ChromeDriverManager(cache_manager=DriverCacheManager(root_dir=os.getenv("WDM_CACHE_PATH"))).install())
        driver = webdriver.Chrome(service=svc, options=opts)
    else:
        driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    driver.implicitly_wait(0)
    log.info("Driver ready (headless=%s)", headless)
    return driver


# ============================================================================
#  HELPERS
# ============================================================================
def get_text(el, driver) -> str:
    try:
        t = driver.execute_script(
            "return arguments[0].innerText || arguments[0].textContent || '';", el
        )
        return (t or "").strip()
    except Exception:
        try:
            return (el.text or "").strip()
        except Exception:
            return ""


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


# ============================================================================
#  URL SLUG PARSER  — PRIMARY data source
# ============================================================================
def parse_slug(href: str) -> dict:
    """
    Extract title, company, location, and job ID from Freshersworld URL slug.

    URL pattern:
      /jobs/{title-words}-jobs-opening-in-{company-words}-at-{location-words}-{id}

    Examples:
      /jobs/data-analyst-internship-jobs-opening-in-nuage-compusys-at-bangalore-2874625
        → title: "Data Analyst Internship", company: "Nuage Compusys",
          location: "Bangalore", id: "2874625"
      /jobs/hr-intern-jobs-opening-in-commversion-solutions-at-andheri-east-mumbai-2871885
        → title: "Hr Intern", company: "Commversion Solutions",
          location: "Andheri East Mumbai", id: "2871885"
    """
    out = {"title": "", "company": "", "location": "", "id": ""}
    try:
        clean_href = href.split("?")[0].rstrip("/")

        # Extract numeric ID at the end
        id_match = re.search(r"-(\d{6,})$", clean_href)
        if not id_match:
            return out
        out["id"] = id_match.group(1)

        # Get slug after /jobs/
        slug_match = re.search(r"/jobs/(.+)", clean_href)
        if not slug_match:
            return out
        slug = slug_match.group(1)

        # Remove trailing ID
        slug = re.sub(r"-\d{6,}$", "", slug)

        # Split on "-jobs-opening-in-" to separate title from company+location
        if "-jobs-opening-in-" in slug:
            parts = slug.split("-jobs-opening-in-", 1)
            title_slug = parts[0]
            rest = parts[1]

            # Split company from location on "-at-"
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
            # Fallback: no standard pattern, use whole slug as title hint
            out["title"] = slug.replace("-", " ").title()

    except Exception as e:
        log.debug("parse_slug error %s: %s", href, e)
    return out


# ============================================================================
#  CARD TEXT PARSER  — SECONDARY data source
# ============================================================================
def parse_card_text(card_text: str, slug_data: dict) -> dict:
    """
    Parse salary, experience, qualifications, posted date from card text.

    Typical Freshersworld card text layout:
      {Title}
      {Company}
      {Location}  {Experience}  {Salary}
      {Qualifications}
      Apply to {Title} Jobs in {Company}...
      Posted: {N} days ago / {N} months ago
    """
    result = {
        "salary":         "N/A",
        "experience":     "N/A",
        "qualifications": "N/A",
        "posted":         "N/A",
        "is_walkin":      False,
        "is_hot":         False,
    }

    # ── Walk-in / Hot job flags ────────────────────────────────────────────
    if re.search(r"\bwalkin\b", card_text, re.IGNORECASE):
        result["is_walkin"] = True
    if re.search(r"\bhot\s*job\b", card_text, re.IGNORECASE):
        result["is_hot"] = True

    # ── Salary ────────────────────────────────────────────────────────────
    # Patterns: "35000 Monthly", "3000 - 5000 Monthly", "Salary not disclosed"
    sal_match = re.search(
        r"(\d[\d,]*(?:\s*-\s*\d[\d,]*)?)\s+Monthly",
        card_text, re.IGNORECASE
    )
    if sal_match:
        result["salary"] = sal_match.group(0).strip()
    elif re.search(r"salary\s+not\s+disclosed", card_text, re.IGNORECASE):
        result["salary"] = "Not Disclosed"

    # ── Experience ────────────────────────────────────────────────────────
    # Patterns: "0 to 3+ Years", "0 Years", "0 to 0.6 Years", "1 to 3 Years"
    exp_match = re.search(
        r"(\d+(?:\.\d+)?)\s+to\s+(\d+\+?)\s+Years?|(\d+(?:\.\d+)?)\s+Years?",
        card_text, re.IGNORECASE
    )
    if exp_match:
        result["experience"] = exp_match.group(0).strip()

    # ── Qualifications ────────────────────────────────────────────────────
    # Qualifications appear as comma-separated degree names
    # Look for lines containing degree abbreviations
    qual_pattern = re.compile(
        r"((?:BE/B\.Tech|BCA|BSc|BBA/BBM|MBA/PGDM|B\.Com|BA|MCA|ME/M\.Tech|"
        r"Diploma|12th Pass|10th Pass|MSc|MA|M\.Com|BBA|BEd|BHM|LLB|CA|CS|"
        r"ICWA|MBBS|BDS|BVSc|BFA|BMS|Other Graduate|Other Course)"
        r"(?:[^.!?\n]{0,200}))",
        re.IGNORECASE
    )
    qual_match = qual_pattern.search(card_text)
    if qual_match:
        raw_qual = qual_match.group(0).strip()
        # Hard-stop at noise phrases to avoid picking up "Apply to..." text
        for stop in ["Apply to", "Posted:", "Save", "View &", "More\n", "More "]:
            if stop in raw_qual:
                raw_qual = raw_qual[:raw_qual.index(stop)]
        result["qualifications"] = clean(raw_qual)[:200]

    # ── Posted date ───────────────────────────────────────────────────────
    # Patterns: "Posted: 5 days ago", "Posted: 1 months ago", "Posted: 2 years ago"
    posted_match = re.search(
        r"Posted:\s*(\d+\s+(?:day|days|month|months|year|years)\s+ago|today|yesterday)",
        card_text, re.IGNORECASE
    )
    if posted_match:
        result["posted"] = posted_match.group(1).strip()

    return result


# ============================================================================
#  PARSE ONE JOB CARD
# ============================================================================
def parse_card(link_el, href: str, driver) -> Optional[dict]:
    """Parse a single job card from its link element."""

    url = href if href.startswith("http") else BASE_URL + href

    # Parse slug for primary data
    slug_data = parse_slug(url)
    if not slug_data["id"]:
        return None

    # Get card container text
    card_text = _get_card_text(link_el, driver)

    # ── Title ─────────────────────────────────────────────────────────────
    # Try link text first (most reliable)
    title = ""
    try:
        t = get_text(link_el, driver)
        if t and 3 < len(t) < 200:
            # Link text often contains the full card — take first line
            first_lines = [l.strip() for l in t.split("\n") if l.strip()]
            if first_lines:
                candidate = first_lines[0]
                # Reject if it looks like metadata
                if not any(skip in candidate.lower() for skip in
                           ["monthly", "years", "posted", "apply", "save"]):
                    title = candidate
    except Exception:
        pass

    # Fallback to slug-parsed title
    if not title:
        title = slug_data.get("title", "")

    if not title:
        return None

    # ── Company ───────────────────────────────────────────────────────────
    company = "N/A"
    # Try h3 element inside card
    try:
        h3_els = link_el.find_elements(By.XPATH, ".//h3")
        if not h3_els:
            # Walk up to find h3 sibling
            parent = link_el.find_element(By.XPATH, "..")
            h3_els = parent.find_elements(By.XPATH, ".//h3")
        if h3_els:
            company = clean(get_text(h3_els[0], driver))
    except Exception:
        pass

    # Fallback to slug-parsed company
    if not company or company == "N/A":
        company = slug_data.get("company", "N/A") or "N/A"

    # ── Location ──────────────────────────────────────────────────────────
    location = "N/A"
    # Check card text for location links (city names appear as links)
    try:
        loc_links = link_el.find_elements(
            By.XPATH, ".//a[contains(@href,'/jobs-in-')]"
        )
        if not loc_links:
            parent = link_el.find_element(By.XPATH, "..")
            loc_links = parent.find_elements(
                By.XPATH, ".//a[contains(@href,'/jobs-in-')]"
            )
        if loc_links:
            cities = [clean(get_text(a, driver)) for a in loc_links[:3]]
            cities = [c for c in cities if c]
            if cities:
                location = ", ".join(cities)
    except Exception:
        pass

    # Fallback to slug-parsed location
    if location == "N/A":
        location = slug_data.get("location", "N/A") or "N/A"

    # Check for WFH in card text
    if "work from home" in card_text.lower():
        location = "Work From Home" if location == "N/A" else location

    # ── Parse remaining fields from card text ─────────────────────────────
    parsed = parse_card_text(card_text, slug_data)

    # ── Skills & role ─────────────────────────────────────────────────────
    full_text = f"{title} {parsed['qualifications']} {card_text}"
    skills = extract_skills(full_text)
    role   = classify_role(title, card_text)

    return {
        "id":             slug_data["id"],
        "title":          title,
        "company":        company,
        "location":       location,
        "salary":         parsed["salary"],
        "experience":     parsed["experience"],
        "qualifications": parsed["qualifications"],
        "posted":         parsed["posted"],
        "is_walkin":      parsed["is_walkin"],
        "is_hot":         parsed["is_hot"],
        "skills":         skills,
        "type":           role,
        "link":           url,
    }


def _get_card_text(link_el, driver) -> str:
    """Walk up DOM to find the single-card container and return its text."""
    try:
        current  = link_el
        best     = ""
        best_len = 0

        for _ in range(10):
            try:
                parent = current.find_element(By.XPATH, "..")
            except Exception:
                break

            tag = ""
            try:
                tag = parent.tag_name.lower()
            except Exception:
                pass
            if tag in ("body", "html", "main", "section", "ul"):
                break

            # Count job links in this container
            try:
                n = len(parent.find_elements(
                    By.XPATH, ".//a[contains(@href,'/jobs/')]"
                ))
            except Exception:
                n = 0

            text = get_text(parent, driver)
            tlen = len(text)

            if n == 1 and tlen > best_len:
                best     = text
                best_len = tlen
                current  = parent
                continue

            if n > 1:
                break

            current = parent

        return best
    except Exception:
        return ""


# ============================================================================
#  BUILD PAGE URL
# ============================================================================
def build_url(city_slug: str, offset: int) -> str:
    """
    Build Freshersworld internship listing URL.

    Main page:  /jobs/category/internship-job-vacancies?&limit=20&offset=N
    City page:  /jobs/category/internship-job-vacancies-{city}?&limit=20&offset=N
    """
    if city_slug:
        base = f"{BASE_URL}/jobs/category/internship-job-vacancies-{city_slug}"
    else:
        base = f"{BASE_URL}/jobs/category/internship-job-vacancies"
    return f"{base}?&limit={PAGE_LIMIT}&offset={offset}"


# ============================================================================
#  SCRAPE ONE PAGE
# ============================================================================
def scrape_page(driver: webdriver.Chrome, url: str, seen_ids: set) -> list:
    """
    Load a listing page, extract all job cards, return new (unseen) items.
    """
    log.info("  → %s", url)
    driver.get(url)

    # Wait for at least one job card link to appear
    try:
        WebDriverWait(driver, PAGE_WAIT).until(
            EC.presence_of_element_located(
                (By.XPATH, "//a[contains(@href,'/jobs/') and contains(@href,'-')]")
            )
        )
    except TimeoutException:
        log.warning("  Timed out waiting for cards")
        return []

    time.sleep(1.5)

    # Scroll to load all lazy-loaded cards
    last_h = driver.execute_script("return document.body.scrollHeight")
    for _ in range(5):
        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(0.2)
        new_h = driver.execute_script("return document.body.scrollHeight")
        if new_h == last_h:
            break
        last_h = new_h
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.3)

    # Collect all unique job links on this page
    all_links = driver.find_elements(By.TAG_NAME, "a")
    seen_on_page: dict = {}  # id → (href, element)

    for a in all_links:
        try:
            href = a.get_attribute("href") or ""
            if not href or "/jobs/" not in href:
                continue
            # Must end with a 6-7 digit numeric ID
            id_match = re.search(r"-(\d{6,7})$", href.split("?")[0])
            if not id_match:
                continue
            # Skip non-job URLs
            if any(skip in href for skip in [
                "/jobs/categories", "/jobs/category", "/jobs-in-",
                "corp.freshersworld", "placement.freshersworld"
            ]):
                continue
            job_id = id_match.group(1)
            if job_id not in seen_on_page:
                seen_on_page[job_id] = (href, a)
        except StaleElementReferenceException:
            continue

    log.info("  Found %d unique job links", len(seen_on_page))

    results = []
    for idx, (job_id, (href, link_el)) in enumerate(seen_on_page.items(), 1):
        if job_id in seen_ids:
            continue  # Already collected globally

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center',behavior:'instant'});",
                link_el
            )
            time.sleep(0.1)

            item = parse_card(link_el, href, driver)
            if item:
                seen_ids.add(job_id)
                results.append(item)
                log.info(
                    "    ✓ [%s] %-42s | %-22s | %-10s | %s",
                    job_id,
                    item["title"][:42],
                    item["company"][:22],
                    item["salary"][:10],
                    item["location"],
                )
        except StaleElementReferenceException:
            log.debug("    Stale element for id %s", job_id)
        except Exception as e:
            log.debug("    Error parsing id %s: %s", job_id, e)

    return results


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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, indent=2, ensure_ascii=False))
        log.info("Saved %d items → %s", len(items), path)
    except Exception as e:
        log.warning("Could not save %s: %s", path, e)


# ============================================================================
#  MAIN
# ============================================================================
def main():
    log.info("Freshersworld Internship Scraper (Selenium)")
    log.info("Target: %d | Cities: %d", TARGET, len(CITY_SLUGS))

    driver    = setup_driver(headless=True)
    all_items: list = []
    seen_ids:  set  = set()

    try:
        for city_slug in CITY_SLUGS:
            city_label = city_slug or "all-india"
            log.info("── City: %s (collected %d so far)", city_label, len(all_items))

            offset = 0
            city_empty_pages = 0

            while True:
                url = build_url(city_slug, offset)
                page_items = scrape_page(driver, url, seen_ids)

                if not page_items:
                    city_empty_pages += 1
                    if city_empty_pages >= 2:
                        log.info("  No new items on 2 consecutive pages — moving to next city")
                        break
                else:
                    city_empty_pages = 0
                    all_items.extend(page_items)
                    log.info("  City %s offset %d: +%d new (total %d)",
                             city_label, offset, len(page_items), len(all_items))

                    # Checkpoint save every 50 items
                    if len(all_items) % 50 < len(page_items):
                        save_json(all_items, OUTPUT_RAW)

                # Stop if target reached
                if len(all_items) >= TARGET:
                    log.info("Target of %d reached.", TARGET)
                    break

                offset += PAGE_LIMIT
                time.sleep(1.0)

            if len(all_items) >= TARGET:
                break

    finally:
        driver.quit()
        log.info("Browser closed.")

    # ── Save raw ──────────────────────────────────────────────────────────────
    save_json(all_items, OUTPUT_RAW)

    # ── Dedup + final save ────────────────────────────────────────────────────
    unique = deduplicate(all_items)
    log.info("After dedup: %d unique internships", len(unique))
    save_json(unique, OUTPUT_FINAL)

    # ── Quality report ────────────────────────────────────────────────────────
    if unique:
        fields = ["company", "location", "salary", "experience", "qualifications", "posted"]
        log.info("=== DATA COMPLETENESS ===")
        for f in fields:
            pct = sum(1 for i in unique if i.get(f, "N/A") not in ("N/A", "")) / len(unique) * 100
            log.info("  %-18s %.0f%%", f, pct)

        log.info("=== ROLE DISTRIBUTION ===")
        for role, cnt in Counter(i.get("type", "Other") for i in unique).most_common():
            log.info("  %-35s %d", role, cnt)

        log.info("=== TOP SKILLS ===")
        all_skills = [s for i in unique for s in i.get("skills", [])]
        for skill, cnt in Counter(all_skills).most_common(10):
            log.info("  %-20s %d", skill, cnt)

        walkin_count = sum(1 for i in unique if i.get("is_walkin"))
        hot_count    = sum(1 for i in unique if i.get("is_hot"))
        log.info("Walk-in jobs: %d | Hot jobs: %d", walkin_count, hot_count)

    print(f"\nTotal internships scraped: {len(unique)}")
    return unique


if __name__ == "__main__":
    main()
