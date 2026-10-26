"""
backfill_deadlines.py — one-off migration: fill `deadlineDate` on stored Internshala listings.

Until now the pipeline only read `deadline_date`, which the scraper path never set, so every stored listing has
deadlineDate null and the site could not show "deadline passed". New scrapes are fixed (pipeline.parse_deadline).
This reads the "APPLY BY" date from each stored Internshala listing's page. Other sources (Unstop, Naukri,
letsintern) only expose the deadline at scrape time and get theirs on the next scrape.

  python backfill_deadlines.py            dry run: prints what would change, writes nothing
  python backfill_deadlines.py --apply    performs the updates

Touches 'internships' and 'internships.mod-unvectorised'. Only documents with deadlineDate null are touched;
safe to re-run. About 0.5 s per listing.
"""

import os
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
    if not apply:
        print("\nDry run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
