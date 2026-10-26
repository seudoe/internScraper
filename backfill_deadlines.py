"""
backfill_deadlines.py — one-off migration: fill `deadlineDate` on stored Internshala listings.

Until now the pipeline only read `deadline_date`, which the scraper path never set, so every stored listing has
deadlineDate null and the site could not show "deadline passed". New scrapes are fixed (pipeline.parse_deadline).
Internshala: the "APPLY BY" date on each stored listing's page. Unstop: `regnRequirements.end_regn_dt` from Unstop's public
search API (listings it no longer returns keep none). Naukri has no deadline; letsintern only shows it on a JavaScript page,
so its listings get one on the next scrape.

  python backfill_deadlines.py            dry run: prints what would change, writes nothing
  python backfill_deadlines.py --apply    performs the updates

Touches 'internships' and 'internships.mod-unvectorised'. Only documents with deadlineDate null are touched;
safe to re-run. About 0.5 s per listing.
"""

import os
import re
import sys
import time
from pathlib import Path

import pymongo
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from pipeline import parse_deadline

COLLECTIONS = ("internships", "internships.mod-unvectorised")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def apply_by(url: str):
    """The page's APPLY BY text, e.g. "14 Aug' 26", or None (closed page, missing, request failed)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.content, "html.parser")
    root = soup.find("div", class_="detail_view") or soup
    for item in root.find_all("div", class_="other_detail_item"):
        heading = item.find("span")
        body = item.find("div", class_="item_body")
        if heading and body and "apply by" in heading.text.lower():
            return body.text.strip()
    return None


_UNSTOP_SEEN: dict = {}   # every opportunity id -> end_regn_dt seen so far (the second collection reuses the first pass)


def unstop_deadlines(wanted: set, max_pages: int = 400, idle_pages: int = 40) -> dict:
    """{opportunity id: end_regn_dt (ISO)} for the wanted ids, paging through Unstop's public search results.
    Stops when everything is found, or after `idle_pages` pages in a row that hold none of the wanted ids (older ones are gone)."""
    def done():
        return {oid: _UNSTOP_SEEN[oid] for oid in wanted if oid in _UNSTOP_SEEN}

    if wanted <= set(_UNSTOP_SEEN):
        return done()
    idle = 0
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get("https://unstop.com/api/public/opportunity/search-result",
                                params={"opportunity": "internships", "per_page": 20, "page": page},
                                headers={**HEADERS, "Accept": "application/json", "Referer": "https://unstop.com/internships"}, timeout=20)
            data = resp.json().get("data", {})
            items = data.get("data", []) if isinstance(data, dict) else []
        except (requests.RequestException, ValueError):
            break
        if not items:
            break
        hit = False
        for it in items:
            oid = str(it.get("id"))
            end = (it.get("regnRequirements") or {}).get("end_regn_dt")
            if end:
                if oid in wanted and oid not in _UNSTOP_SEEN:
                    hit = True
                _UNSTOP_SEEN[oid] = end
        idle = 0 if hit else idle + 1
        if wanted <= set(_UNSTOP_SEEN) or idle >= idle_pages:
            break
        time.sleep(0.3)
    return done()


def main() -> None:
    apply = "--apply" in sys.argv
    here = Path(__file__).resolve().parent
    for env in (here / ".env", here / ".env.local"):
        if env.exists():
            load_dotenv(env)
    uri = os.getenv("MONGODB_URI")
    if not uri:
        sys.exit("MONGODB_URI is not set")

    db = pymongo.MongoClient(uri, serverSelectionTimeoutMS=10000)[os.getenv("MONGODB_DB_NAME", "ifind")]
    # One pass over Unstop's search results for the listings of BOTH collections (the older ones sit deeper in the results)
    unstop_wanted = set()
    for name in COLLECTIONS:
        for doc in db[name].find({"deadlineDate": None, "applyLink": {"$regex": r"^https?://(www\.)?unstop\.com/"}}, {"applyLink": 1}):
            m = re.search(r"-(\d+)/?$", doc["applyLink"])
            if m:
                unstop_wanted.add(m.group(1))
    if unstop_wanted:
        print(f"Looking up {len(unstop_wanted)} Unstop listings on unstop.com (can take a few minutes) ...", flush=True)
        unstop_deadlines(unstop_wanted)

    for name in COLLECTIONS:
        col = db[name]
        found = skipped = 0
        query = {"deadlineDate": None, "applyLink": {"$regex": r"^https?://(www\.)?internshala\.com/"}}
        docs = list(col.find(query, {"applyLink": 1}))
        print(f"{name}: {len(docs)} Internshala listings without a deadline")
        for doc in docs:
            time.sleep(0.5)
            deadline = parse_deadline({"deadline_text": apply_by(doc["applyLink"]) or ""})
            if not deadline:
                skipped += 1
                continue
            found += 1
            if apply:
                col.update_one({"_id": doc["_id"], "deadlineDate": None}, {"$set": {"deadlineDate": deadline}})
        print(f"  deadline found for {found}, none for {skipped}{'' if apply else ' (dry run)'}")

        # Unstop: one pass over the search API for all stored Unstop listings
        by_id = {}
        for doc in col.find({"deadlineDate": None, "applyLink": {"$regex": r"^https?://(www\.)?unstop\.com/"}}, {"applyLink": 1}):
            m = re.search(r"-(\d+)/?$", doc["applyLink"])
            if m:
                by_id[m.group(1)] = doc["_id"]
        hits = unstop_deadlines(set(by_id)) if by_id else {}
        n = 0
        for oid, iso in hits.items():
            deadline = parse_deadline({"deadline": iso})
            if deadline:
                n += 1
                if apply:
                    col.update_one({"_id": by_id[oid], "deadlineDate": None}, {"$set": {"deadlineDate": deadline}})
        print(f"{name}: {len(by_id)} Unstop listings without a deadline; found one for {n}{'' if apply else ' (dry run)'}")
    if not apply:
        print("\nDry run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
