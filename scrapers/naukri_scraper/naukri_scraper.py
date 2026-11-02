"""
Naukri.com Internship Scraper
==============================
Scrapes internships from https://www.naukri.com/internship-jobs

URL: https://www.naukri.com/{keyword}-internship-jobs[-{page}]
"""

import re
import json
import os
from pathlib import Path
import time
import logging
from typing import Optional

from selenium import webdriver
from core.chrome import start_chrome
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
log = logging.getLogger("naukri-intern")

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_KEYWORD  = ""        # e.g. "software developer", "marketing", "" for all
SEARCH_LOCATION = ""        # e.g. "bangalore", "" for all India
PAGES           = 8         # ~300 internships at ~40/page
OUTPUT_DIR      = Path(__file__).resolve().parents[2] / "data" / "scrapers"   # <project>/data/scrapers
OUTPUT_RAW      = OUTPUT_DIR / "naukri_raw.json"
OUTPUT_FINAL    = OUTPUT_DIR / "naukri_internships.json"

# ── Skills ────────────────────────────────────────────────────────────────────
SKILLS_LIST = [
    "python", "java", "c++", "javascript", "react", "node", "html", "css",
    "sql", "excel", "machine learning", "data analysis", "figma", "aws",
    "azure", "docker", "git", "angular", "vue", "django", "flask", "mongodb",
    "mysql", "postgresql", "typescript", "kotlin", "swift", "flutter",
    "android", "ios", "linux", "bash", "tableau", "power bi",
]

ROLE_MAP = {
    "Software Development": ["software", "developer", "engineer", "backend", "frontend", "full stack", "web", "mobile"],
    "Data Science / AI":    ["data science", "machine learning", "ai", "deep learning", "nlp", "analytics"],
    "DevOps / Cloud":       ["devops", "cloud", "aws", "azure", "kubernetes", "docker", "infrastructure"],
    "Design":               ["design", "ui", "ux", "figma", "graphic"],
    "Marketing":            ["marketing", "seo", "digital marketing", "content", "social media"],
    "Sales / BD":           ["sales", "business development", "account manager"],
    "HR":                   ["hr", "human resources", "recruitment", "talent"],
    "Finance":              ["finance", "accounting", "audit", "tax"],
    "Testing / QA":         ["qa", "quality assurance", "testing", "automation testing"],
}

INDIAN_CITIES = [
    "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune",
    "chennai", "kolkata", "ahmedabad", "noida", "gurgaon", "gurugram",
    "jaipur", "lucknow", "chandigarh", "indore", "bhopal", "kochi",
    "coimbatore", "nagpur", "surat", "vadodara", "visakhapatnam", "patna",
    "bhubaneswar", "dehradun", "mysore", "mysuru", "thane", "navi-mumbai",
    "faridabad", "ghaziabad", "agra", "meerut", "rajkot", "nashik",
    "aurangabad", "ranchi", "howrah", "jabalpur", "jodhpur", "raipur",
    "kota", "guwahati", "solapur", "hubli", "tiruchirappalli", "bareilly",
    "tiruppur", "aligarh", "jalandhar", "salem", "warangal", "guntur",
    "bhiwandi", "saharanpur", "gorakhpur", "bikaner", "amravati",
    "jamshedpur", "bhilai", "cuttack", "nellore", "bhavnagar", "durgapur",
    "asansol", "rourkela", "nanded", "kolhapur", "ajmer", "akola",
    "gulbarga", "jamnagar", "ujjain", "siliguri", "jhansi", "ulhasnagar",
    "jammu", "sangli", "mangalore", "erode", "belgaum", "ambattur",
    "tirunelveli", "malegaon", "gaya", "jalgaon", "udaipur", "secunderabad",
    "trivandrum", "thiruvananthapuram", "kozhikode", "calicut", "thrissur",
    "madurai", "vellore", "pondicherry", "puducherry",
    "work-from-home", "remote", "multiple-locations", "pan-india",
]

LOCATION_MAP = {
    "work-from-home": "Remote / WFH", "remote": "Remote / WFH",
    "multiple-locations": "Multiple Locations", "pan-india": "Pan India",
    "bengaluru": "Bengaluru", "bangalore": "Bangalore",
    "navi-mumbai": "Navi Mumbai", "gurugram": "Gurugram", "gurgaon": "Gurgaon",
    "thiruvananthapuram": "Thiruvananthapuram", "trivandrum": "Thiruvananthapuram",
    "calicut": "Kozhikode", "puducherry": "Puducherry", "pondicherry": "Puducherry",
    "secunderabad": "Secunderabad", "mysuru": "Mysuru", "mysore": "Mysore",
}

COMPANY_INDICATORS = {
    "limited", "ltd", "pvt", "private", "technologies", "technology",
    "solutions", "systems", "services", "consulting", "consultancy",
    "software", "infotech", "infosystems", "global", "india",
    "international", "enterprises", "group", "labs", "studio",
    "digital", "analytics", "ventures", "networks", "telecom",
    "finance", "bank", "insurance", "healthcare", "pharma",
    "industries", "manufacturing", "engineering", "research",
    "foundation", "trust",
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
        svc = Service(os.getenv("CHROMEDRIVER_PATH") or ChromeDriverManager(cache_manager=DriverCacheManager(root_dir=os.getenv("WDM_CACHE_PATH"))).install())
        driver = start_chrome(opts, svc)
    else:
        driver = start_chrome(opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    driver.implicitly_wait(0)
    log.info("Driver ready (headless=%s)", headless)
    return driver


# ============================================================================
#  URL BUILDER
# ============================================================================
def build_url(keyword: str, location: str, page: int) -> str:
    """
    Naukri internship search URLs:
      All internships:          /internship-jobs
      By keyword:               /{kw}-internship-jobs
      By keyword + location:    /{kw}-internship-jobs-in-{city}
      Pagination:               append -{page}
    """
    if keyword.strip():
        slug = keyword.strip().lower().replace(" ", "-")
        if location.strip():
            loc = location.strip().lower().replace(" ", "-")
            base = f"https://www.naukri.com/{slug}-internship-jobs-in-{loc}"
        else:
            base = f"https://www.naukri.com/{slug}-internship-jobs"
    else:
        if location.strip():
            loc = location.strip().lower().replace(" ", "-")
            base = f"https://www.naukri.com/internship-jobs-in-{loc}"
        else:
            base = "https://www.naukri.com/internship-jobs"
    return f"{base}-{page}" if page > 1 else base


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
#  URL SLUG PARSER  — PRIMARY data source
# ============================================================================
def parse_slug(href: str) -> dict:
    """
    Extract title_hint, company, location, experience from the URL slug.
    Naukri internship URLs: /job-listings-{title}-{company}-{city}-{exp}-{id}
    Example:
      /job-listings-software-developer-intern-siemens-bengaluru-0-to-1-years-090426929138
      -> company: Siemens, location: Bengaluru, experience: 0-1 Yrs
    """
    out = {"title_hint": "", "company": "", "location": "", "experience": ""}
    try:
        clean = href.split("?")[0].split("#")[0]
        m = re.search(r"/job-listings-(.+)", clean)
        if not m:
            return out
        slug = m.group(1)
        slug = re.sub(r"-\d{10,}$", "", slug)

        # Extract experience (months or years)
        exp = ""
        exp_range = re.search(r"(\d+)-to-(\d+)-(years?|months?)", slug)
        exp_single = re.search(r"(\d+)-(years?|months?)", slug)
        if exp_range:
            lo, hi, unit = exp_range.group(1), exp_range.group(2), exp_range.group(3)
            unit_s = "Months" if "month" in unit else "Yrs"
            exp = f"{lo}-{hi} {unit_s}"
            slug = slug[:exp_range.start()].rstrip("-")
        elif exp_single:
            n, unit = exp_single.group(1), exp_single.group(2)
            unit_s = "Months" if "month" in unit else "Yrs"
            exp = f"{n} {unit_s}"
            slug = slug[:exp_single.start()].rstrip("-")
        out["experience"] = exp

        # Find city (scan from end, try 2-word then 1-word)
        parts = slug.split("-")
        city_slug = ""
        city_end = len(parts)
        for i in range(len(parts) - 1, 0, -1):
            two = f"{parts[i-1]}-{parts[i]}"
            if two in INDIAN_CITIES:
                city_slug = two
                city_end = i - 1
                break
            if parts[i] in INDIAN_CITIES:
                city_slug = parts[i]
                city_end = i
                break

        if city_slug:
            out["location"] = LOCATION_MAP.get(
                city_slug, city_slug.replace("-", " ").title()
            )

        # Split remaining into title + company
        pre = parts[:city_end]
        if len(pre) >= 4:
            best_split = None
            for idx in range(len(pre) - 1, 0, -1):
                if pre[idx].lower() in COMPANY_INDICATORS:
                    best_split = max(1, idx - 3)
                    break
            if best_split is not None:
                title_p, company_p = pre[:best_split], pre[best_split:]
            else:
                split = max(2, len(pre) - 3)
                title_p, company_p = pre[:split], pre[split:]
        else:
            title_p, company_p = pre, []

        out["title_hint"] = " ".join(title_p).replace("-", " ").title()
        out["company"]    = " ".join(company_p).replace("-", " ").title()
    except Exception as e:
        log.debug("parse_slug error %s: %s", href, e)
    return out


# ============================================================================
#  CARD TEXT  — SECONDARY data source
# ============================================================================
def get_card_text(link_el, driver) -> str:
    """Walk up DOM from link to find the single-card container."""
    try:
        current = link_el
        best, best_len = "", 0
        for _ in range(12):
            try:
                parent = current.find_element(By.XPATH, "..")
            except Exception:
                break
            tag = ""
            try:
                tag = parent.tag_name.lower()
            except Exception:
                pass
            if tag in ("body", "html", "main"):
                break
            try:
                n = len(parent.find_elements(
                    By.XPATH, ".//a[contains(@href,'job-listings')]"
                ))
            except Exception:
                n = 0
            text = get_text(parent, driver)
            tlen = len(text)
            if n == 1 and tlen > best_len:
                best, best_len = text, tlen
                current = parent
                continue
            if n > 1:
                break
            current = parent
        return best
    except Exception:
        return ""


# ============================================================================
#  PARSE ONE INTERNSHIP
# ============================================================================
def parse_internship(link_el, href: str, driver) -> Optional[dict]:
    url       = href if href.startswith("http") else "https://www.naukri.com" + href
    slug_data = parse_slug(url)
    card_text = get_card_text(link_el, driver)
    lines     = [l.strip() for l in card_text.split("\n") if l.strip()]

    # TITLE
    title = ""
    try:
        t = get_text(link_el, driver)
        if t and 3 < len(t) < 150:
            title = t
    except Exception:
        pass
    if not title:
        skip = {"lpa", "apply", "years", "yrs", "openings", "view", "rating", "reviews"}
        for line in lines[:5]:
            if (3 < len(line) < 150
                    and not any(s in line.lower() for s in skip)
                    and not re.search(r"^\d+(\.\d+)?$", line)):
                title = line
                break
    if not title:
        title = slug_data.get("title_hint", "")
    if not title:
        return None

    # COMPANY
    company = ""
    bad = {"lpa", "apply", "years", "yrs", "openings", "view", "salary",
           "experience", "location", "posted", "rating", "reviews",
           "hot", "urgent", "new", "featured", "fresher"}
    for line in lines[1:8]:
        if (line != title and 1 < len(line) < 100
                and not any(b in line.lower() for b in bad)
                and not re.search(r"^\d+(\.\d+)?$", line)
                and not re.search(r"\d+\s*(yr|year|month|lpa|lac)", line.lower())
                and not re.search(r"^\d+[-\u2013]\d+", line)):
            company = line
            break
    if not company:
        company = slug_data.get("company", "") or "N/A"

    # LOCATION
    location = ""
    lower = card_text.lower()
    if any(r in lower for r in ["work from home", "remote", "wfh"]):
        location = "Remote / WFH"
    else:
        city_re = (
            r"\b(Mumbai|Delhi|Bangalore|Bengaluru|Chennai|Hyderabad|Pune|"
            r"Kolkata|Ahmedabad|Noida|Gurgaon|Gurugram|Jaipur|Lucknow|"
            r"Chandigarh|Indore|Bhopal|Kochi|Coimbatore|Nagpur|Surat|"
            r"Vadodara|Visakhapatnam|Patna|Bhubaneswar|Dehradun|Mysore|"
            r"Thane|Navi Mumbai|Faridabad|Ghaziabad|Agra|Meerut|Rajkot|"
            r"Nashik|Aurangabad|Ranchi|Howrah|Jabalpur|Jodhpur|Raipur|"
            r"Kota|Guwahati|Solapur|Hubli|Tiruchirappalli|Bareilly|"
            r"Mysuru|Tiruppur|Aligarh|Jalandhar|Salem|Warangal|Guntur|"
            r"Bhiwandi|Saharanpur|Gorakhpur|Bikaner|Amravati|Jamshedpur|"
            r"Bhilai|Cuttack|Nellore|Bhavnagar|Durgapur|Asansol|Rourkela|"
            r"Nanded|Kolhapur|Ajmer|Akola|Gulbarga|Jamnagar|Ujjain|"
            r"Siliguri|Jhansi|Ulhasnagar|Jammu|Sangli|Mangalore|Erode|"
            r"Belgaum|Ambattur|Tirunelveli|Malegaon|Gaya|Jalgaon|Udaipur|"
            r"Secunderabad|Trivandrum|Thiruvananthapuram|Kozhikode|Calicut|"
            r"Thrissur|Madurai|Vellore|Pondicherry|Puducherry)\b"
        )
        found = re.findall(city_re, card_text, re.IGNORECASE)
        if found:
            seen_c, unique = set(), []
            for c in found:
                if c.lower() not in seen_c:
                    seen_c.add(c.lower())
                    unique.append(c)
            location = ", ".join(unique[:3])
    if not location:
        location = slug_data.get("location", "") or "N/A"

    # DURATION / EXPERIENCE
    duration = ""
    em = re.search(r"(\d+)\s*[-\u2013]\s*(\d+)\s*(?:Months?|Yrs?|Years?)", card_text, re.IGNORECASE)
    if em:
        duration = em.group(0).strip()
    elif re.search(r"\b(fresher|0\s*year|entry\s*level)\b", card_text, re.IGNORECASE):
        duration = "Fresher / 0 Yrs"
    else:
        se = re.search(r"(\d+)\+?\s*(?:Months?|Yrs?|Years?)", card_text, re.IGNORECASE)
        if se:
            duration = se.group(0).strip()
    if not duration:
        duration = slug_data.get("experience", "") or "N/A"

    # STIPEND
    stipend = "N/A"
    sm = re.search(
        r"(?:\u20b9\s*)?(\d+(?:\.\d+)?)\s*[-\u2013]\s*(\d+(?:\.\d+)?)"
        r"\s*(?:Lacs?|LPA|L\.?P\.?A\.?|Lakhs?|K)\s*(?:PA|P\.?A\.?)?",
        card_text, re.IGNORECASE
    )
    if sm:
        stipend = sm.group(0).strip()
    elif re.search(r"not\s+disclosed|unpaid", card_text, re.IGNORECASE):
        stipend = "Unpaid / Not Disclosed"
    else:
        ss = re.search(r"\u20b9\s*[\d,]+", card_text)
        if ss:
            stipend = ss.group(0).strip()

    # POSTED DATE
    posted = "N/A"
    pm = re.search(r"(\d+)\s*(day|hour|week|month)s?\s*ago", card_text, re.IGNORECASE)
    if pm:
        posted = pm.group(0).strip()
    elif re.search(r"just\s+now|today", card_text, re.IGNORECASE):
        posted = "Today"
    elif re.search(r"yesterday", card_text, re.IGNORECASE):
        posted = "Yesterday"

    # DESCRIPTION
    description = "N/A"
    skip_d = {"lpa", "apply", "view", "openings", "salary", "experience", "rating", "reviews"}
    for line in lines:
        if (len(line) > 40 and line != title and line != company
                and not any(s in line.lower() for s in skip_d)
                and not re.search(r"\d+\s*(yr|year|month|lpa|lac)", line.lower())
                and not re.search(r"^\d+(\.\d+)?$", line)):
            description = line
            break

    skills = extract_skills(f"{title} {card_text}")
    role   = classify_role(title, card_text)

    return {
        "title":       title,
        "company":     company,
        "location":    location,
        "stipend":     stipend,
        "duration":    duration,
        "posted":      posted,
        "skills":      skills,
        "type":        role,
        "link":        url,
        "description": description,
    }


# ============================================================================
#  SCRAPE ONE PAGE
# ============================================================================
def scrape_page(driver: webdriver.Chrome, page_num: int) -> list:
    url = build_url(SEARCH_KEYWORD, SEARCH_LOCATION, page_num)
    log.info("Page %d -> %s", page_num, url)
    driver.get(url)

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.TAG_NAME, "a"))
        )
    except TimeoutException:
        log.warning("Page %d: timed out", page_num)
        return []

    time.sleep(3)

    last_h = driver.execute_script("return document.body.scrollHeight")
    for _ in range(15):
        driver.execute_script("window.scrollBy(0, 500);")
        time.sleep(0.3)
        new_h = driver.execute_script("return document.body.scrollHeight")
        if new_h == last_h:
            break
        last_h = new_h
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)

    all_links = driver.find_elements(By.TAG_NAME, "a")
    log.info("Found %d total links on page %d", len(all_links), page_num)

    seen: dict = {}
    for a in all_links:
        try:
            href = a.get_attribute("href") or ""
            if not href or "naukri.com" not in href:
                continue
            if "job-listings" not in href:
                continue
            m = re.search(r"-(\d{10,12})$", href.split("?")[0])
            if not m:
                continue
            job_id = m.group(1)
            if job_id not in seen:
                seen[job_id] = (href, a)
        except StaleElementReferenceException:
            continue

    log.info("Unique internship IDs on page %d: %d", page_num, len(seen))
    if not seen:
        log.warning("No internship links on page %d", page_num)
        return []

    results = []
    for idx, (job_id, (href, link_el)) in enumerate(seen.items(), 1):
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center',behavior:'instant'});",
                link_el
            )
            time.sleep(0.3)
            item = parse_internship(link_el, href, driver)
            if item:
                results.append(item)
                log.info(
                    "  [%d/%d] OK %-40s | %-22s | %-10s | %s",
                    idx, len(seen),
                    item["title"][:40],
                    item["company"][:22],
                    item["duration"],
                    item["location"],
                )
        except StaleElementReferenceException:
            pass
        except Exception as e:
            log.debug("  [%d/%d] error: %s", idx, len(seen), e)

    log.info("Page %d done: %d extracted", page_num, len(results))
    return results


# ============================================================================
#  DEDUPLICATE + SAVE
# ============================================================================
def deduplicate(items: list) -> list:
    seen, out = set(), []
    for item in items:
        m = re.search(r"-(\d{10,12})$", item.get("link", "").split("?")[0])
        key = m.group(1) if m else item.get("link", "")
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def save_json(items: list, path: str):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(items, indent=2, ensure_ascii=False))
        log.info("Saved %d items -> %s", len(items), path)
    except Exception as e:
        log.warning("Could not save %s: %s", path, e)


# ============================================================================
#  MAIN
# ============================================================================
def main():
    log.info("=== Naukri Internship Scraper ===")
    log.info("Keyword: '%s' | Location: '%s' | Pages: %d",
             SEARCH_KEYWORD or "all", SEARCH_LOCATION or "All India", PAGES)

    driver = setup_driver(headless=True)
    all_items: list = []
    consecutive_empty = 0

    try:
        for page in range(1, PAGES + 1):
            data = scrape_page(driver, page)
            if not data:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log.info("3 consecutive empty pages, stopping.")
                    break
            else:
                consecutive_empty = 0
                all_items.extend(data)
                log.info("Cumulative: %d (page %d/%d)", len(all_items), page, PAGES)
                if page % 5 == 0:
                    save_json(all_items, OUTPUT_RAW)
            time.sleep(2)
    finally:
        driver.quit()
        log.info("Browser closed.")

    save_json(all_items, OUTPUT_RAW)
    unique = deduplicate(all_items)
    log.info("After dedup: %d", len(unique))
    save_json(unique, OUTPUT_FINAL)

    fields = ["company", "location", "stipend", "duration", "posted", "description"]
    log.info("=== DATA COMPLETENESS ===")
    for f in fields:
        if unique:
            pct = sum(1 for i in unique if i.get(f, "N/A") not in ("N/A", "")) / len(unique) * 100
            log.info("  %-15s %.0f%%", f, pct)

    from collections import Counter
    log.info("=== ROLE DISTRIBUTION ===")
    for role, cnt in Counter(i.get("type", "Other") for i in unique).most_common():
        log.info("  %-30s %d", role, cnt)

    print(f"\nTotal internships scraped: {len(unique)}")
    return unique


if __name__ == "__main__":
    main()
