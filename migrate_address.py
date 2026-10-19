"""
migrate_address.py — one-off migration: flat city/state/country -> address[].

Before, an internship stored `city`, `state` and `country` as three top-level fields. The schema now has
`address: [{city, state, country}, ...]`, so one internship can list several locations. This script rewrites
every old document: it builds `address` from the three fields (skipping empty ones) and removes them.
Documents that already have `address` are only cleaned of any leftover flat fields.

  python migrate_address.py            dry run: prints what would change, writes nothing
  python migrate_address.py --apply    performs the updates

Touches 'internships', 'internships.mod-unvectorised' and 'internships.this-platform'. Run it ONCE, before
deploying the new code (the code no longer reads the flat fields); it is safe to re-run.
"""

import os
import sys
from collections import Counter
from pathlib import Path

import pymongo
from dotenv import load_dotenv

COLLECTIONS = ("internships", "internships.mod-unvectorised", "internships.this-platform")
FLAT = ("city", "state", "country")


def build_address(doc: dict) -> list:
    """[{city, state, country}] from the flat fields, or [] when the document had no location."""
    entry = {k: (str(doc[k]).strip() or None) if doc.get(k) else None for k in FLAT}
    return [entry] if entry["city"] or entry["state"] else []


def update_for(doc: dict):
    """The Mongo update that migrates one document, or None when it is already migrated."""
    has_flat = any(k in doc for k in FLAT)
    if "address" in doc and not has_flat:
        return None
    update: dict = {"$unset": {k: "" for k in FLAT}}
    if "address" not in doc:
        update["$set"] = {"address": build_address(doc)}
    return update


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
        ops = []
        for doc in col.find({"$or": [{"address": {"$exists": False}}, *({k: {"$exists": True}} for k in FLAT)]}):
            update = update_for(doc)
            if update is None:
                continue
            plan["with_location" if update.get("$set", {}).get("address") else "no_location"] += 1
            ops.append(pymongo.UpdateOne({"_id": doc["_id"]}, update))
        print(f"{name}: {len(ops)} to migrate {dict(plan)}")
        if apply and ops:
            res = col.bulk_write(ops, ordered=False)
            print(f"  updated {res.modified_count}")
    if not apply:
        print("\nDry run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
