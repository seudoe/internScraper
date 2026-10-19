"""
script.py — Master scraper orchestrator

Runs all scrapers sequentially, accumulates results in memory,
checkpoints to a JSON file after each scraper (so nothing is lost
if execution is interrupted), then pushes everything through the
moderation pipeline into MongoDB.

Usage (standalone):
    python script.py [--skip-scrape] [--scrapers github,internshala,indeed,naukri,unstop,freshersworld,letsintern]

Flags:
    --skip-scrape   Skip running scrapers; push existing checkpoint file to DB
    --scrapers      Comma-separated list of scrapers to run (default: all)
"""

import sys
import os
import json
import logging
import argparse
from services import vectoriser_url
from usage_log import BudgetExceeded
import time
from pathlib import Path
from datetime import datetime, timezone

# ─── Paths ────────────────────────────────────────────────────────────────────
SCRAPER_DIR = Path(__file__).parent
SCRAPERS_DIR = SCRAPER_DIR / "scrapers"
CHECKPOINT_FILE = SCRAPER_DIR / "checkpoint_internships.json"

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("script")

RUNS_COLLECTION = "scrape.runs"

# ─── Default max per scraper ──────────────────────────────────────────────────
DEFAULT_MAX = 300

# ─── Scraper registry ─────────────────────────────────────────────────────────
# Each entry: id, label, module, fn, source, max_param (how the scraper accepts a limit)
#   max_param = "arg"        → pass max as first positional arg to fn()
#   max_param = "TARGET"     → patch module.TARGET before calling main()
#   max_param = "PAGES"      → patch module.PAGES (naukri: ~40/page, so pages = max//40)
#   max_param = None         → no cap supported (letsintern scrapes all URLs it finds)
SCRAPER_REGISTRY = [
    {
        "id":        "github",
        "label":     "GitHub (2026 SWE Jobs)",
        "module":    "scrapers.github_scraper",
        "fn":        "scrape_github_internships",
        "source":    "web_scraping",
        "max_param": "arg",
    },
    {
        "id":        "internshala",
        "label":     "Internshala",
        "module":    "scrapers.internshala_scraper",
        "fn":        "scrape_internshala_internships",
        "source":    "web_scraping",
        "max_param": "arg",
    },
    {
        "id":        "indeed",
        "label":     "Indeed",
        "module":    "scrapers.indeed_scraper",
        "fn":        "scrape_indeed_internships",
        "source":    "web_scraping",
        "max_param": "arg",
    },
    {
        "id":        "naukri",
        "label":     "Naukri",
        "module":    "scrapers.naukri_scraper.naukri_scraper",
        "fn":        "main",
        "source":    "web_scraping",
        "max_param": "PAGES",   # naukri uses PAGES; ~40 results/page
    },
    {
        "id":        "unstop",
        "label":     "Unstop",
        "module":    "scrapers.unstop_scraper.unstop_scraper",
        "fn":        "main",
        "source":    "web_scraping",
        "max_param": "TARGET",
    },
    {
        "id":        "freshersworld",
        "label":     "Freshersworld",
        "module":    "scrapers.freshersworld_scraper.freshersworld_scraper",
        "fn":        "main",
        "source":    "web_scraping",
        "max_param": "TARGET",
    },
    {
        "id":        "letsintern",
        "label":     "LetsIntern",
        "module":    "scrapers.letsintern_scraper.letsintern_scraper",
        "fn":        "main",
        "source":    "web_scraping",
        "max_param": None,      # scrapes all URLs it finds; no numeric cap
    },
]


# ─── Checkpoint helpers ───────────────────────────────────────────────────────

def load_checkpoint() -> list:
    """Load previously scraped internships from the checkpoint file."""
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("📂 Loaded %d internships from checkpoint: %s", len(data), CHECKPOINT_FILE)
            return data
        except Exception as e:
            log.warning("Could not load checkpoint: %s", e)
    return []


def save_checkpoint(internships: list):
    """Save all accumulated internships to the checkpoint file."""
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(internships, f, indent=2, ensure_ascii=False, default=str)
        log.info("💾 Checkpoint saved: %d internships → %s", len(internships), CHECKPOINT_FILE)
    except Exception as e:
        log.warning("Could not save checkpoint: %s", e)


# ─── Scraper runner ───────────────────────────────────────────────────────────

def run_scraper(scraper: dict, max_items: int = DEFAULT_MAX) -> tuple[list, str | None]:
    """
    Dynamically import and run a scraper module.
    Applies max_items according to each scraper's max_param strategy.
    Returns (raw internship dicts, error message or None).
    """
    scraper_dir_str = str(SCRAPER_DIR)
    if scraper_dir_str not in sys.path:
        sys.path.insert(0, scraper_dir_str)

    try:
        import importlib
        mod = importlib.import_module(scraper["module"])
        fn  = getattr(mod, scraper["fn"])
        max_param = scraper.get("max_param")

        if max_param == "arg":
            # fn accepts max as first positional argument
            result = fn(max_items)
        elif max_param == "TARGET":
            # patch module-level TARGET before calling main()
            mod.TARGET = max_items
            result = fn()
        elif max_param == "PAGES":
            # naukri: ~40 results per page, round up
            pages = max(1, (max_items + 39) // 40)
            mod.PAGES = pages
            log.info("  [naukri] max_items=%d → PAGES=%d", max_items, pages)
            result = fn()
        else:
            # No cap supported — run as-is
            result = fn()

        if result is None:
            return [], None
        if isinstance(result, list):
            return result, None
        log.warning("  Scraper '%s' returned unexpected type: %s", scraper["id"], type(result))
        return [], f"unexpected return type {type(result).__name__}"
    except BudgetExceeded as e:
        log.error("  ⏸ Scraper '%s' stopped: %s", scraper["id"], e)
        return [], f"budget_exceeded: {e}"[:500]
    except Exception as e:
        log.error("  ❌ Scraper '%s' failed: %s", scraper["id"], e)
        return [], f"{type(e).__name__}: {e}"[:500]


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _open_staging():
    """Connect to MongoDB and return (client, staging collection). Run logs: client["ifind"][RUNS_COLLECTION]."""
    from dotenv import load_dotenv
    import pymongo

    env_path = SCRAPER_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()  # fallback to CWD

    mongo_uri = os.environ.get("MONGODB_URI")
    if not mongo_uri:
        raise RuntimeError("MONGODB_URI not set in environment")

    log.info("\n🔌 Connecting to MongoDB...")
    client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
    col = client["ifind"]["internships.mod-unvectorised"]
    log.info("✅ Connected\n")
    return client, col


def _push_batch(items: list, scraper_id: str, col) -> dict:
    """Run one scraper's items through dedup + scam detection and save to staging."""
    from pipeline import push_to_pipeline

    # `source` on every stored listing is the scraper id (github, indeed, ...), not the generic channel
    log.info("▶ Moderation pipeline: %s (%d items)", scraper_id, len(items))
    stats = push_to_pipeline(items, scraper_id, col, label=scraper_id)
    log.info(
        "  → saved:%d  dupes:%d  rejected:%d  errors:%d",
        stats["saved"], stats["duplicate"], stats["rejected"], stats["errors"],
    )
    return stats


def _trigger_vectorizer() -> None:
    """Auto-approved listings are vectorized + graph-indexed by the vectorizer service (best effort)."""
    vec_url = vectoriser_url()
    if not vec_url:
        return
    try:
        import requests
        requests.post(f"{vec_url}/vectorize-hnsw", json={"background": True}, timeout=15)
        log.info("🧭 Vectorizer triggered for auto-approved listings")
    except Exception as exc:
        log.warning("Vectorizer trigger failed (%s) — approved items will be picked up on the next run", exc)


# ─── Run log (collection `scrape.runs`) ───────────────────────────────────────

class RunLog:
    """
    One document per scrape run, one entry in `sources` per scraper. Written after every
    scraper, so a crash still leaves everything finished so far. Best effort: a logging
    failure must never stop scraping.
    """

    def __init__(self, client, trigger: str, requested: dict, job_id: str | None = None):
        self.col = client["ifind"][RUNS_COLLECTION]
        self.t0 = time.monotonic()
        self.id = None
        try:
            self.id = self.col.insert_one({
                "jobId": job_id,
                "trigger": trigger,                    # "scrape" | "checkpoint"
                "status": "running",
                "startedAt": datetime.now(timezone.utc),
                "finishedAt": None,
                "durationSeconds": None,
                "requested": requested,                # {scraper_id: max_items}
                "vectorizerConfigured": bool(vectoriser_url()),
                "totals": {},
                "sources": [],
            }).inserted_id
        except Exception as exc:
            log.warning("Run log disabled (%s)", exc)

    def add_source(self, src: dict, totals: dict) -> None:
        if self.id is None:
            return
        try:
            self.col.update_one({"_id": self.id}, {"$push": {"sources": src}, "$set": {"totals": totals}})
        except Exception as exc:
            log.warning("Run log write failed (%s)", exc)

    def flag_budget_exceeded(self) -> None:
        """The daily AI budget stopped a scraper in this run."""
        if self.id is None:
            return
        try:
            self.col.update_one({"_id": self.id}, {"$set": {"budgetExceeded": True}})
        except Exception as exc:
            log.warning("Run log write failed (%s)", exc)

    def finish(self, status: str, totals: dict, error: str | None = None) -> None:
        if self.id is None:
            return
        try:
            self.col.update_one({"_id": self.id}, {"$set": {
                "status": status, "error": error, "totals": totals,
                "finishedAt": datetime.now(timezone.utc),
                "durationSeconds": round(time.monotonic() - self.t0, 1),
            }})
        except Exception as exc:
            log.warning("Run log finish failed (%s)", exc)


def _source_doc(scraper: dict, max_items, started: datetime, scraped: int, scrape_s: float,
                scraper_error: str | None, stats: dict | None, pipeline_s: float,
                pipeline_error: str | None) -> dict:
    stats = stats or {}
    return {
        "id": scraper["id"],
        "label": scraper["label"],
        "maxRequested": max_items,
        "startedAt": started,
        "scrapeSeconds": round(scrape_s, 1),
        "pipelineSeconds": round(pipeline_s, 1),
        "scraped": scraped,
        "scraperError": scraper_error,         # scraper crashed / returned junk
        "pipelineError": pipeline_error,       # scoring/saving step crashed
        "emptyResult": scraped == 0,
        "saved": stats.get("saved", 0),
        "duplicate": stats.get("duplicate", 0),
        "rejected": stats.get("rejected", 0),
        "errors": stats.get("errors", 0) + (scraped if pipeline_error else 0),
        **(stats.get("detail") or {}),
    }


# ─── Main orchestrator ────────────────────────────────────────────────────────

def run_streaming(
    selected_ids: list[str],
    max_values: dict[str, int] | None = None,
    on_progress=None,
    job_id: str | None = None,
) -> dict:
    """
    Scrape → scam-check → save, one scraper at a time. Each scraper's results are saved to
    staging as soon as that scraper finishes, so a crash or restart loses at most the scraper
    that was running, never the ones already done.

    max_values:  per-scraper overrides, e.g. {"github": 50, "naukri": 200}
    on_progress: optional callback(totals_dict) after every scraper (used for live job status)
    """
    active = [s for s in SCRAPER_REGISTRY if s["id"] in selected_ids]

    log.info("\n╔══════════════════════════════════════════════════╗")
    log.info("║         iFind Scraper Orchestrator               ║")
    log.info("╚══════════════════════════════════════════════════╝")
    log.info("Scrapers: %s\n", ", ".join(s["label"] for s in active))

    client, col = _open_staging()
    totals = {"saved": 0, "duplicate": 0, "rejected": 0, "errors": 0, "scraped": 0}
    scraped_all: list = []   # kept only as an offline checkpoint for --skip-scrape
    runlog = RunLog(client, "scrape", {s["id"]: (max_values or {}).get(s["id"], DEFAULT_MAX) for s in active}, job_id)
    run_status, run_error = "done", None

    try:
        for scraper in active:
            max_items = (max_values or {}).get(scraper["id"], DEFAULT_MAX)
            log.info("━━━ %s (max: %d) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", scraper["label"], max_items)
            started = datetime.now(timezone.utc)
            t0 = time.monotonic()
            items, scraper_error = run_scraper(scraper, max_items=max_items)
            scrape_s = time.monotonic() - t0
            if scraper_error and scraper_error.startswith("budget_exceeded"):
                runlog.flag_budget_exceeded()

            if not items:
                log.warning("  No internships returned from %s", scraper["label"])
                runlog.add_source(_source_doc(scraper, max_items, started, 0, scrape_s, scraper_error, None, 0, None), dict(totals))
                continue

            for item in items:
                item["_scraper_source"] = scraper["source"]
                item["_scraper_id"]     = scraper["id"]
            log.info("  ✓ %s returned %d internships", scraper["label"], len(items))
            totals["scraped"] += len(items)
            scraped_all.extend(items)
            save_checkpoint(scraped_all)

            stats, pipeline_error = None, None
            t1 = time.monotonic()
            try:
                stats = _push_batch(items, scraper["id"], col)
                for k in ("saved", "duplicate", "rejected", "errors"):
                    totals[k] += stats[k]
                if stats["saved"]:
                    _trigger_vectorizer()
            except Exception as exc:
                log.exception("  💥 Pipeline failed for %s: %s", scraper["id"], exc)
                totals["errors"] += len(items)
                pipeline_error = f"{type(exc).__name__}: {exc}"[:500]
            runlog.add_source(
                _source_doc(scraper, max_items, started, len(items), scrape_s, scraper_error, stats,
                            time.monotonic() - t1, pipeline_error),
                dict(totals),
            )

            if on_progress:
                on_progress(dict(totals))
    except BaseException as exc:
        run_status, run_error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        runlog.finish(run_status, dict(totals), run_error)
        client.close()

    return totals


# ─── Offline re-push (checkpoint) ─────────────────────────────────────────────

def push_all_to_db(internships: list, job_id: str | None = None) -> dict:
    """Push an already-scraped list (checkpoint) through the moderation pipeline."""
    from collections import defaultdict

    client, col = _open_staging()
    totals = {"saved": 0, "duplicate": 0, "rejected": 0, "errors": 0}

    by_scraper: dict[str, list] = defaultdict(list)
    for item in internships:
        by_scraper[item.get("_scraper_id", "unknown")].append(item)

    runlog = RunLog(client, "checkpoint", {k: len(v) for k, v in by_scraper.items()}, job_id)
    run_status, run_error = "done", None
    labels = {s["id"]: s["label"] for s in SCRAPER_REGISTRY}
    try:
        for scraper_id, batch in by_scraper.items():
            started = datetime.now(timezone.utc)
            t1 = time.monotonic()
            stats = _push_batch(batch, scraper_id, col)
            for k in totals:
                totals[k] += stats[k]
            runlog.add_source(
                _source_doc({"id": scraper_id, "label": labels.get(scraper_id, scraper_id)}, None, started,
                            len(batch), 0.0, None, stats, time.monotonic() - t1, None),
                dict(totals),
            )
    except BaseException as exc:
        run_status, run_error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        runlog.finish(run_status, dict(totals), run_error)
        client.close()
    if totals["saved"]:
        _trigger_vectorizer()
    return totals


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main(
    selected_ids: list[str] | None = None,
    skip_scrape: bool = False,
    max_values: dict[str, int] | None = None,
    on_progress=None,
    job_id: str | None = None,
) -> dict:
    """
    Main entry point — can be called from CLI or from app.py.
    max_values: per-scraper overrides, e.g. {"github": 50, "naukri": 200}
    """
    if selected_ids is None:
        selected_ids = [s["id"] for s in SCRAPER_REGISTRY]

    if skip_scrape:
        log.info("⏭  --skip-scrape: loading from checkpoint only")
        internships = load_checkpoint()
        if not internships:
            log.warning("No checkpoint found and --skip-scrape was set. Nothing to push.")
            return {"saved": 0, "duplicate": 0, "rejected": 0, "errors": 0}
        totals = push_all_to_db(internships, job_id=job_id)
    else:
        totals = run_streaming(selected_ids, max_values=max_values, on_progress=on_progress, job_id=job_id)

    log.info("\n━━━ Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("✅ Saved to DB:   %d", totals["saved"])
    log.info("⏭  Duplicates:   %d", totals["duplicate"])
    log.info("❌ Rejected:      %d", totals["rejected"])
    log.info("💥 Errors:        %d", totals["errors"])
    log.info("\n🎯 Open the Moderation tab in the dashboard to review.")

    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="iFind scraper orchestrator")
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip scraping; push existing checkpoint file to DB",
    )
    parser.add_argument(
        "--scrapers",
        type=str,
        default=",".join(s["id"] for s in SCRAPER_REGISTRY),
        help="Comma-separated list of scrapers to run",
    )
    args = parser.parse_args()

    selected = [s.strip() for s in args.scrapers.split(",") if s.strip()]
    main(selected_ids=selected, skip_scrape=args.skip_scrape)
