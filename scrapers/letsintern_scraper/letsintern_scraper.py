"""
LetsIntern Scraper (letsintern.in)
===================================
Scrapes internships from https://letsintern.in/current-internships/

Strategy:
  1. Load the listing page and collect all internship detail URLs
  2. For each URL, visit the detail page and extract fields from plain text
  3. No class/id assumptions — pure Selenium + text parsing
  4. spaCy used optionally for NER (company/location fallback)

Fields extracted:
  title, company, location, stipend, duration, start_date,
  apply_by, skills, type, link, description
"""

import re
import json
import time
import logging
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

try:
    from webdriver_manager.chrome import ChromeDriverManager
    USE_WDM = True
except ImportError:
    USE_WDM = False

try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
    SPACY_ENABLED = True
    print("spaCy loaded successfully")
except Exception as e:
    print(f"spaCy not available: {e}")
    SPACY_ENABLED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("letsintern")

# ── Config ────────────────────────────────────────────────────────────────────
# All pages that list internships/jobs on LetsIntern
LISTING_PAGES = [
    "https://letsintern.in/current-internships/",
    "https://letsintern.in/jobs/",
    "https://letsintern.in/",
]
MAX_ITEMS    = None       # stop after this many internships (None = no cap; set by /check)
OUTPUT_RAW   = "internships_raw.json"
OUTPUT_FINAL = "internships.json"

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
        svc = Service(ChromeDriverManager().install())
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
#  STEP 1: COLLECT ALL INTERNSHIP URLS FROM LISTING PAGE
# ============================================================================
def collect_internship_urls(driver: webdriver.Chrome, listing_url: str) -> list:
    """
    Load a listing page and collect all unique internship/job detail URLs.
    Structure-independent: finds links by URL pattern (letsintern.in/slug/).
    """
    log.info("Loading listing page: %s", listing_url)
    driver.get(listing_url)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "a"))
        )
    except TimeoutException:
        log.warning("Listing page timed out")
        return []

    time.sleep(3)

    # Scroll to load all cards
    last_h = driver.execute_script("return document.body.scrollHeight")
    for _ in range(10):
        driver.execute_script("window.scrollBy(0, 600);")
        time.sleep(0.4)
        new_h = driver.execute_script("return document.body.scrollHeight")
        if new_h == last_h:
            break
        last_h = new_h
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)

    # Collect all internship detail links from the listing page.
    # An internship link is any letsintern.in URL that:
    #   - has a slug path (not just the domain)
    #   - is not a known navigation/utility page
    #   - has no query string or fragment
    SKIP_PATHS = {
        "", "/", "about-us", "contact-us", "contact-2", "current-internships",
        "blog", "career-blog-news", "privacy-policy", "privacy-policy-2",
        "terms-conditions", "sitemap", "author", "category", "tag", "page",
        "internships-2026-guide", "jobs", "join-as-employer",
        "cancellation-refund-policy",
    }

    all_links = driver.find_elements(By.TAG_NAME, "a")
    seen, urls = set(), []

    for a in all_links:
        try:
            href = (a.get_attribute("href") or "").strip()

            # Must be a letsintern.in link (not social share, mailto, etc.)
            if not href.startswith("https://letsintern.in/"):
                continue

            # Strip query string and fragment
            clean_href = href.split("?")[0].split("#")[0].rstrip("/")

            # Extract path after domain
            path = clean_href.replace("https://letsintern.in", "").strip("/")

            # Skip empty paths and known nav pages
            if not path or path in SKIP_PATHS:
                continue

            # Must be a simple slug (letters, numbers, hyphens only)
            if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", path):
                continue

            final_url = f"https://letsintern.in/{path}/"
            if final_url not in seen:
                seen.add(final_url)
                urls.append(final_url)

        except StaleElementReferenceException:
            continue

    log.info("Found %d unique internship URLs", len(urls))
    return urls


# ============================================================================
#  STEP 2: PARSE ONE INTERNSHIP DETAIL PAGE
# ============================================================================
def parse_detail_page(driver: webdriver.Chrome, url: str) -> Optional[dict]:
    """
    Visit an internship detail page and extract all fields from plain text.
    No class/id assumptions.
    """
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            return None
        time.sleep(1.5)

        body = driver.find_element(By.TAG_NAME, "body")
        text = get_text(body, driver)

        if not text or len(text) < 50:
            return None

        # Skip 404 pages and non-internship pages
        if re.search(r"page doesn.t seem to exist|404|not found", text[:200], re.IGNORECASE):
            return None

        lines = [l.strip() for l in text.split("\n") if l.strip()]

        # ── TITLE ─────────────────────────────────────────────────────────────
        # Try <h1> first (most reliable on detail pages)
        title = "N/A"
        try:
            h1s = driver.find_elements(By.TAG_NAME, "h1")
            for h1 in h1s:
                t = get_text(h1, driver)
                if t and 3 < len(t) < 150:
                    title = t
                    break
        except Exception:
            pass

        if title == "N/A":
            # Try page title tag
            try:
                pt = driver.title.split("|")[0].strip()
                if pt and len(pt) > 3:
                    title = pt
            except Exception:
                pass

        if title == "N/A":
            # First meaningful line
            for line in lines[:5]:
                if 3 < len(line) < 150:
                    title = line
                    break

        if title == "N/A":
            return None

        # Skip non-internship pages
        skip_titles = {"about letsintern", "contact", "privacy policy", "terms"}
        if any(s in title.lower() for s in skip_titles):
            return None

        # ── COMPANY ───────────────────────────────────────────────────────────
        # LetsIntern is the platform itself — company is "LetsIntern"
        # but some listings mention a specific company
        company = "LetsIntern"
        if SPACY_ENABLED:
            try:
                doc = nlp(text[:800])
                for ent in doc.ents:
                    if ent.label_ == "ORG" and ent.text.lower() not in ("letsintern", "india"):
                        c = ent.text.strip()
                        # Must be a single clean line (no newlines)
                        if len(c) > 2 and "\n" not in c:
                            company = c
                            break
            except Exception:
                pass

        # Also check for explicit "Company: X" pattern on job listing pages
        company_match = re.search(r"company[:\s]+([^\n]{3,80})", text, re.IGNORECASE)
        if company_match:
            val = company_match.group(1).strip()
            # Clean up trailing punctuation and parentheses content
            val = re.sub(r"\s*\(.*?\)\s*$", "", val).strip()
            if 2 < len(val) < 80:
                company = val

        # ── LOCATION ──────────────────────────────────────────────────────────
        location = "Remote / WFH"  # LetsIntern is primarily online/remote
        lower = text.lower()
        if "online" in lower or "remote" in lower or "work from home" in lower or "wfh" in lower:
            location = "Remote / WFH"
        else:
            # Check for city mentions
            city_re = (
                r"\b(Mumbai|Delhi|Bangalore|Bengaluru|Chennai|Hyderabad|Pune|"
                r"Kolkata|Ahmedabad|Noida|Gurgaon|Gurugram|Jaipur|Lucknow|"
                r"Chandigarh|Indore|Bhopal|Kochi|Coimbatore|Nagpur|Surat|"
                r"Vadodara|Visakhapatnam|Patna|Bhubaneswar|Dehradun)\b"
            )
            found = re.findall(city_re, text, re.IGNORECASE)
            if found:
                seen_c, unique = set(), []
                for c in found:
                    if c.lower() not in seen_c:
                        seen_c.add(c.lower())
                        unique.append(c)
                location = ", ".join(unique[:3])

        # ── STIPEND ───────────────────────────────────────────────────────────
        stipend = "N/A"

        # Priority 1: Look for explicit ₹ range (e.g. "₹10,000 – ₹16,000 per month")
        sm = re.search(
            r"₹\s*[\d,]+\s*(?:[-–]\s*₹?\s*[\d,]+)?\s*(?:per\s*month|/month|pm|p\.m\.)?",
            text, re.IGNORECASE
        )
        if sm:
            stipend = sm.group(0).strip()

        # Priority 2: Rs./INR pattern (e.g. "Rs. 8000/- to Rs.10,000/-")
        # Must have digits immediately after Rs. to avoid matching "Hrs," etc.
        if stipend == "N/A":
            sm2 = re.search(
                r"\bRs\.?\s*\d[\d,]*(?:/-)?(?:\s*(?:to|[-–])\s*Rs\.?\s*\d[\d,]*(?:/-)?)?",
                text, re.IGNORECASE
            )
            if sm2:
                stipend = sm2.group(0).strip()

        # Priority 3: "Stipend Range: X" pattern
        if stipend == "N/A":
            sr = re.search(r"stipend\s*range[:\s]+([^\n]{5,60})", text, re.IGNORECASE)
            if sr:
                val = sr.group(1).strip()
                if len(val) < 60:
                    stipend = val

        # Priority 4: "Minimum Salary: X" pattern
        if stipend == "N/A":
            ms = re.search(r"minimum\s*salary\s*[:\s]+([^\n]{3,30})", text, re.IGNORECASE)
            mx = re.search(r"maximum\s*salary\s*[:\s]+([^\n]{3,30})", text, re.IGNORECASE)
            if ms and mx:
                stipend = f"{ms.group(1).strip()} - {mx.group(1).strip()}"
            elif ms:
                stipend = ms.group(1).strip()

        # Priority 5: Only fall back to "Performance Based" if no amount found
        if stipend == "N/A":
            if re.search(r"performance.based", text, re.IGNORECASE):
                stipend = "Performance Based"
            elif re.search(r"unpaid", text, re.IGNORECASE):
                stipend = "Unpaid"

        # ── DURATION ──────────────────────────────────────────────────────────
        duration = "N/A"
        # LetsIntern offers 1-6 months
        dm = re.search(
            r"(\d+(?:\s*[-–,/]\s*\d+)?)\s*(?:Months?|Weeks?)",
            text, re.IGNORECASE
        )
        if dm:
            duration = dm.group(0).strip()
        else:
            # "Choose Your Internship Duration — 1, 2, 3, 4, 5, or 6 Months"
            dm2 = re.search(r"duration[:\s–—]+([^\n]{3,60})", text, re.IGNORECASE)
            if dm2:
                val = dm2.group(1).strip()
                if len(val) < 80:
                    duration = val

        # ── START DATE ────────────────────────────────────────────────────────
        start_date = "N/A"
        sd = re.search(
            r"(?:start\s*date|starting)[:\s]*([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})",
            text, re.IGNORECASE
        )
        if sd:
            start_date = sd.group(1).strip()
        else:
            # "WEDNESDAY, MAY 6TH, 2026"
            sd2 = re.search(
                r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s*"
                r"([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})",
                text, re.IGNORECASE
            )
            if sd2:
                start_date = sd2.group(0).strip()

        # ── APPLY BY ──────────────────────────────────────────────────────────
        apply_by = "N/A"
        ab = re.search(
            r"(?:last\s*date|apply\s*by|deadline)[:\s]*([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})",
            text, re.IGNORECASE
        )
        if ab:
            apply_by = ab.group(1).strip()
        else:
            ab2 = re.search(
                r"(?:last\s*date|apply\s*by|deadline)[:\s]*([^\n]{5,40})",
                text, re.IGNORECASE
            )
            if ab2:
                val = ab2.group(1).strip()
                if len(val) < 50:
                    apply_by = val

        # ── DESCRIPTION ───────────────────────────────────────────────────────
        description = "N/A"
        skip_d = {"skip to content", "follow us", "click here", "play now",
                  "facebook", "twitter", "linkedin", "instagram", "youtube",
                  "whatsapp", "registration form", "first", "last"}
        for line in lines:
            if (len(line) > 50
                    and line.lower() != title.lower()
                    and not any(s in line.lower() for s in skip_d)
                    and not re.search(r"^\d+(\.\d+)?$", line)):
                description = line
                break

        # ── SKILLS & ROLE ─────────────────────────────────────────────────────
        skills = extract_skills(f"{title} {description} {text}")
        role   = classify_role(title, text)

        return {
            "title":       title,
            "company":     company,
            "location":    location,
            "stipend":     stipend,
            "duration":    duration,
            "start_date":  start_date,
            "apply_by":    apply_by,
            "skills":      skills,
            "type":        role,
            "link":        url,
            "description": description,
        }

    except Exception as e:
        log.debug("parse_detail_page error for %s: %s", url, e)
        return None


# ============================================================================
#  MAIN SCRAPE FLOW
# ============================================================================
def scrape(driver: webdriver.Chrome) -> list:
    # Step 1: collect all internship URLs from ALL listing pages
    all_urls_seen = set()
    urls = []

    for listing_page in LISTING_PAGES:
        page_urls = collect_internship_urls(driver, listing_page)
        for u in page_urls:
            if u not in all_urls_seen:
                all_urls_seen.add(u)
                urls.append(u)

    log.info("Total unique internship URLs across all pages: %d", len(urls))

    if not urls:
        log.warning("No internship URLs found. Check if site is accessible.")
        return []

    # Step 2: visit each detail page and extract data
    results = []
    for idx, url in enumerate(urls, 1):
        if MAX_ITEMS and len(results) >= MAX_ITEMS:
            break
        try:
            item = parse_detail_page(driver, url)
            if item:
                results.append(item)
                log.info(
                    "  [%d/%d] OK %-45s | %-20s | %-12s | %s",
                    idx, len(urls),
                    item["title"][:45],
                    item["company"][:20],
                    item["duration"],
                    item["location"],
                )
            else:
                log.debug("  [%d/%d] skipped: %s", idx, len(urls), url)

            time.sleep(1)  # Polite pause between detail pages

        except Exception as e:
            log.debug("  [%d/%d] error: %s — %s", idx, len(urls), url, e)

    log.info("Scraped %d internships from %d URLs", len(results), len(urls))
    return results


# ============================================================================
#  SAVE
# ============================================================================
def save_json(items: list, path: str):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, indent=2, ensure_ascii=False))
        log.info("Saved %d items -> %s", len(items), path)
    except Exception as e:
        log.warning("Could not save %s: %s", path, e)


# ============================================================================
#  MAIN
# ============================================================================
def main():
    log.info("=== LetsIntern Scraper ===")
    log.info("Sources: %s", ", ".join(LISTING_PAGES))

    driver = setup_driver(headless=True)
    all_items: list = []

    try:
        all_items = scrape(driver)
    finally:
        driver.quit()
        log.info("Browser closed.")

    # Save raw
    save_json(all_items, OUTPUT_RAW)

    # Quality report
    fields = ["company", "location", "stipend", "duration", "start_date", "apply_by", "description"]
    log.info("=== DATA COMPLETENESS ===")
    for f in fields:
        if all_items:
            pct = sum(1 for i in all_items if i.get(f, "N/A") not in ("N/A", "")) / len(all_items) * 100
            log.info("  %-15s %.0f%%", f, pct)

    from collections import Counter
    log.info("=== ROLE DISTRIBUTION ===")
    for role, cnt in Counter(i.get("type", "Other") for i in all_items).most_common():
        log.info("  %-30s %d", role, cnt)

    # Save final (same as raw since no dedup needed — each URL is unique)
    save_json(all_items, OUTPUT_FINAL)
    print(f"\nTotal internships scraped: {len(all_items)}")
    return all_items


if __name__ == "__main__":
    main()
