r"""
run_on_db.py
------------
Read-only MongoDB pipeline runner for scam_detector.
Fetches all internship documents from the MongoDB 'internships' collection,
runs the scam_detector pipeline, outputs the results to scored_internships.json,
and prints a formatted summary directly to the CLI.

Usage:
    cd c:/Users/4dmin/Downloads/iFind30/IpdFetaures
    python -m scam_detector.run_on_db

Output:
    c:/Users/4dmin/Downloads/iFind30/IpdFetaures/scam_detector/scored_internships.json
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# ── Load Environment Variables ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
ENV_LOCAL = BASE_DIR / ".env.local"
ENV = BASE_DIR / ".env"

if ENV_LOCAL.exists():
    load_dotenv(ENV_LOCAL)
elif ENV.exists():
    load_dotenv(ENV)

# Also check parent directory (ifind/.env.local)
PARENT_ENV = BASE_DIR.parent.parent / "ifind" / ".env.local"
if PARENT_ENV.exists():
    load_dotenv(PARENT_ENV)

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise SystemExit("MONGODB_URI not set (put it in .env.local)")
DB_NAME = os.getenv("MONGODB_DB_NAME", "ifind")
COLLECTION_NAME = os.getenv("MONGODB_COLLECTION", "internships.mod-unvectorised")
OUTPUT_JSON = BASE_DIR / "scored_internships.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run_on_db")

# Import normalizers from run_on_scraper
try:
    from scam_detector.run_on_scraper import (
        _parse_stipend,
        _parse_duration,
        _parse_location,
        _parse_date,
    )
except ImportError:
    from run_on_scraper import (
        _parse_stipend,
        _parse_duration,
        _parse_location,
        _parse_date,
    )


def _format_date(val: Any) -> str | None:
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, str):
        if re.match(r"^\d{4}-\d{2}-\d{2}", val):
            return val[:10]
        return _parse_date(val)
    return None


def adapt_db_doc(doc: dict[str, Any], index: int) -> dict[str, Any]:
    """Map a MongoDB document → pipeline schema."""
    doc_id = str(doc.get("_id") or doc.get("id") or f"db-{index}")

    name = doc.get("name") or doc.get("title") or ""
    company = doc.get("company") or ""
    apply_link = doc.get("applyLink") or doc.get("link") or doc.get("apply_link") or doc.get("url") or ""
    summary = doc.get("summary") or doc.get("description") or ""

    skills = doc.get("skills") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]

    field = doc.get("field")
    if isinstance(field, str):
        field = [field]

    raw_stipend = doc.get("stipend")
    if isinstance(raw_stipend, dict):
        stipend = raw_stipend
    else:
        stipend = _parse_stipend(raw_stipend if isinstance(raw_stipend, str) else None)

    raw_duration = doc.get("duration")
    if isinstance(raw_duration, dict):
        duration = raw_duration
    else:
        duration = _parse_duration(raw_duration if isinstance(raw_duration, str) else None)

    raw_location = doc.get("location")
    if isinstance(raw_location, dict):
        location = raw_location
    elif isinstance(raw_location, str):
        location = _parse_location(raw_location)
    else:
        # Stored documents carry `address: [{city, state, country}, ...]`; the detector scores the first one
        first = (doc.get("address") or [{}])[0] or {}
        location = {
            "isRemote": doc.get("isRemote", False),
            "city": first.get("city"),
            "state": first.get("state"),
            "country": first.get("country"),
        }

    deadline = _format_date(doc.get("deadlineDate") or doc.get("apply_by") or doc.get("deadline"))
    published = _format_date(doc.get("datePublished") or doc.get("start_date") or doc.get("createdAt"))

    return {
        "_id": doc_id,
        "name": name,
        "company": company,
        "applyLink": apply_link,
        "summary": summary,
        "skills": skills,
        "field": field,
        "stipend": stipend,
        "duration": duration,
        "isRemote": location.get("isRemote", False),
        "city": location.get("city"),
        "state": location.get("state"),
        "country": location.get("country"),
        "deadlineDate": deadline,
        "datePublished": published or "2026-01-01",
        "source": doc.get("source") or "mongodb_internships",
        "isActive": doc.get("isActive", True),
        "openings": doc.get("openings"),
        "perks": doc.get("perks"),
    }


def display_cli_summary(scored: list[dict[str, Any]]) -> None:
    """Print a clean CLI summary of the evaluation results."""
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    total = len(scored)
    if not total:
        print("\nNo records scored.")
        return

    clears = [r for r in scored if r.get("decision") == "clear"]
    reviews = [r for r in scored if r.get("decision") == "review"]
    blocks = [r for r in scored if r.get("decision") == "block"]

    avg_score = sum(r.get("scam_score", 0) for r in scored) / total
    avg_conf = sum(r.get("confidence", 0) for r in scored) / total

    print("\n" + "=" * 65)
    print(" === SCAM DETECTOR READ-ONLY DB EVALUATION REPORT ===")
    print("=" * 65)
    print(f" Total Internships Scanned : {total}")
    print(f" Average Scam Score       : {avg_score:.2f} / 100")
    print(f" Average Model Confidence  : {avg_conf:.2%}")
    print("-" * 65)
    print(f" [CLEAR]  (Low Risk)     : {len(clears):4d}  ({len(clears)/total*100:5.1f}%)")
    print(f" [REVIEW] (Medium Risk)  : {len(reviews):4d}  ({len(reviews)/total*100:5.1f}%)")
    print(f" [BLOCK]  (High Risk)    : {len(blocks):4d}  ({len(blocks)/total*100:5.1f}%)")
    print("=" * 65)

    # Show top flagged / blocked listings
    flagged = blocks + sorted(reviews, key=lambda x: x.get("scam_score", 0), reverse=True)[:10]
    if flagged:
        print("\n=== TOP FLAGGED / HIGH RISK LISTINGS ===")
        print("-" * 65)
        for i, item in enumerate(flagged[:10], start=1):
            decision = item.get("decision", "unknown").upper()
            score = item.get("scam_score", 0)
            name = item.get("name") or "Untitled"
            company = item.get("company") or "Unknown Co."
            flags = item.get("explanation_summary") or item.get("scam_flags") or []

            tag = "[BLOCK]" if decision == "BLOCK" else "[REVIEW]"
            print(f"{i:2d}. {tag} Score: {score:5.1f} | {name} @ {company}")
            if flags:
                print(f"    Flags: {flags}")
        print("=" * 65 + "\n")


def main() -> None:
    import pymongo

    log.info("Connecting to MongoDB (READ-ONLY)...")
    client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)

    try:
        db = client.get_default_database(default=DB_NAME)
    except Exception:
        db = client[DB_NAME]

    collection = db[COLLECTION_NAME]

    log.info("Fetching internship documents from collection '%s.%s'...", db.name, collection.name)
    docs = list(collection.find())
    log.info("Fetched %d documents from MongoDB.", len(docs))

    if not docs:
        log.warning("No documents found in collection '%s.%s'. Exiting.", db.name, collection.name)
        return

    adapted = [adapt_db_doc(doc, i) for i, doc in enumerate(docs)]
    log.info("Adapted %d records for scam_detector pipeline.", len(adapted))

    try:
        from scam_detector.pipeline import process_records, write_records
    except ImportError:
        from .pipeline import process_records, write_records

    log.info("Running scam_detector pipeline on %d records…", len(adapted))
    scored = process_records(adapted)

    # Save to scored_internships.json
    write_records(OUTPUT_JSON, scored)
    log.info("Saved %d scored records to JSON → %s", len(scored), OUTPUT_JSON)

    # Display CLI report summary
    display_cli_summary(scored)


if __name__ == "__main__":
    main()
