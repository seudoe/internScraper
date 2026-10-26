"""
backfill_source.py — one-off migration: set every scraped listing's `source` from its apply link.

`source` is the site the student applies on: internshala, indeed, naukri, unstop, freshersworld, letsintern, github,
or "web" when the link goes to a company's own site (or any host we do not recognise). Older documents carry the
legacy "web_scraping" or the id of the scraper that found them (even when the link leaves for a company site).
"telegram" and "ifind" listings are never touched.

  python backfill_source.py            dry run: prints what would change, writes nothing
  python backfill_source.py --apply    performs the updates

Touches collections 'internships' and 'internships.mod-unvectorised'. Run it once; it is safe to re-run.
"""

import os
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import pymongo
from dotenv import load_dotenv

from pipeline import source_for_link

COLLECTIONS = ("internships", "internships.mod-unvectorised")


def main() -> None:
    apply = "--apply" in sys.argv
    here = Path(__file__).resolve().parent
    for env in (here / ".env", here / ".env.local", here.parent / "ifind" / ".env.local"):
        if env.exists():
            load_dotenv(env)
    uri = os.getenv("MONGODB_URI")
    if not uri:
        sys.exit("MONGODB_URI is not set")

    db = pymongo.MongoClient(uri, serverSelectionTimeoutMS=10000)[os.getenv("MONGODB_DB_NAME", "ifind")]
    for name in COLLECTIONS:
        col = db[name]
        plan: Counter = Counter()
        web_hosts: Counter = Counter()
        updates: dict = {}
        for doc in col.find({"source": {"$nin": ["telegram", "ifind"]}}, {"applyLink": 1, "source": 1}):
            src = source_for_link(doc.get("applyLink"))
            if src == doc.get("source"):
                continue
            plan[src] += 1
            updates.setdefault(src, []).append(doc["_id"])
            if src == "web":
                web_hosts[(urlparse(doc.get("applyLink") or "").hostname or "?")] += 1
        print(f"{name}: would set {dict(plan)}")
        if web_hosts:
            print(f"  most common hosts that become \"web\": {web_hosts.most_common(8)}")
        if apply:
            for src, ids in updates.items():
                res = col.update_many({"_id": {"$in": ids}}, {"$set": {"source": src}})
                print(f"  {src}: updated {res.modified_count}")
    if not apply:
        print("\nDry run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
