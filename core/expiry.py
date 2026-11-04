"""
Expired internships: scraped listings whose application deadline has passed are moved from 'internships' to
'internships.expired' (kept so students' applied / saved history still resolves), then the vectoriser is told to drop
them from the HNSW graph and refresh recommendations. Run after every scrape job (app.py _run_job).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("expiry")

LIVE_COLLECTION = "internships"
EXPIRED_COLLECTION = "internships.expired"


def deadline_passed(value, now: Optional[datetime] = None) -> bool:
    """Same rule as the vectoriser and ifind: naive ISO strings are UTC, a date-only value lasts to the end of that day."""
    if not value:
        return False
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    if d.hour == d.minute == d.second == 0:
        d = d.replace(hour=23, minute=59, second=59)
    return d < (now or datetime.now(timezone.utc))


def expire_internships(db=None) -> dict:
    """Move expired listings to 'internships.expired', then ask the vectoriser to purge them from the graph."""
    if db is None:
        import pymongo
        uri = os.environ.get("MONGODB_URI")
        if not uri:
            return {"moved": 0, "error": "MONGODB_URI not set"}
        db = pymongo.MongoClient(uri, serverSelectionTimeoutMS=10000)[os.environ.get("MONGODB_DB_NAME", "ifind")]

    now = datetime.now(timezone.utc)
    moved = 0
    for d in db[LIVE_COLLECTION].find({"deadlineDate": {"$ne": None}}, {"deadlineDate": 1}):
        if not deadline_passed(d.get("deadlineDate"), now):
            continue
        full = db[LIVE_COLLECTION].find_one({"_id": d["_id"]})
        if full is None:
            continue
        full.pop("tfidf_vector", None)
        full.pop("bert_vector", None)
        db[EXPIRED_COLLECTION].replace_one({"_id": d["_id"]}, {**full, "expiredAt": now}, upsert=True)
        db[LIVE_COLLECTION].delete_one({"_id": d["_id"]})
        moved += 1
    log.info("🗄  Expired internships moved to '%s': %d", EXPIRED_COLLECTION, moved)

    # always tell the vectoriser: it also removes expired employer posts / stale nodes from the graph
    result = {"moved": moved, "vectoriserNotified": False}
    from core.services import vectoriser_url
    url = vectoriser_url()
    if url:
        try:
            import requests
            res = requests.post(f"{url}/expire-internships", timeout=120)
            res.raise_for_status()
            result["vectoriserNotified"] = True
        except Exception as exc:
            log.warning("Vectoriser expiry call failed (%s): the next recommendation run sweeps again", exc)
    return result
