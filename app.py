"""
app.py — FastAPI service for the iFind scraper pipeline

Designed for deployment on Hugging Face Spaces (or any container).

WHY ASYNC / BACKGROUND JOBS?
─────────────────────────────
Scraping all sources can take 5–30+ minutes.
Hugging Face Spaces (and most HTTP proxies) time out after ~60 seconds.
Returning a 200 only after scraping finishes would always time out.

Solution: fire-and-forget background jobs.
  POST /scrape          → starts a background job, returns 202 + job_id immediately
  GET  /scrape/{job_id} → poll for status / results
  GET  /health          → liveness check

Endpoints:
──────────
POST /scrape
  Body (JSON, all optional):
    {
      "scrapers": ["github", "internshala", "indeed", "naukri", "unstop", "freshersworld", "letsintern"],
      "skip_scrape": false
    }
  Response 202:
    { "job_id": "...", "status": "running", "message": "..." }

GET /scrape/{job_id}
  Response 200:
    {
      "job_id": "...",
      "status": "running" | "done" | "failed",
      "started_at": "...",
      "finished_at": "...",
      "stats": { "saved": N, "duplicate": N, "rejected": N, "errors": N },
      "error": "..." | null
    }

POST /telegram/webhook   (Telegram bot; see scrapers/telegram_bot/bot.py for the TELEGRAM_* settings)

POST /process        (platform path: internships posted on iFind itself)
  Body:   { "source": "ifind", "internship": {...} }   or   { "source": "ifind", "internships": [{...}, ...] }
  Runs the same validation + scam detection as the scraper path and returns the verdicts.
  Synchronous, max 50 items per call. For source "ifind" send each item's ifind document `id` and `companyVerified`
  (auto-approval needs exactly true). The verdict is written onto the document in internships.this-platform while it is
  still pending_review, and auto-approved posts are sent to the vectoriser (/vectorize-platform) in the background.
  Response 200: { "source", "count", "summary", "detectorFailed", "results": [ {index, id, outcome, moderationStatus, score, persisted, vectorizerTriggered, ...} ] }
  If INTERNSCRAPER_API_KEY is set, send it as the x-api-key header.

GET /health
  Response 200: { "status": "ok" }
"""

import os
import uuid
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Logging ──────────────────────────────────────────────────────────────────
from ifind_log import RequestLogMiddleware, capture_print, get_logger, setup

setup("internscraper")
capture_print()  # scrapers and the scam detector use bare print(); route it through the same format
log = get_logger("app")

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="iFind Scraper API",
    description="Scrapes internship listings and pushes them through the moderation pipeline into MongoDB.",
    version="1.0.0",
)

# Allowed browser origins: MAIN_URL (the main project, comma-separated; set http://localhost:3000 for local dev).
from services import main_urls
ALLOWED_ORIGINS = main_urls()

app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Telegram bot (webhook): internships forwarded from WhatsApp, source "telegram"
from scrapers.telegram_bot.bot import router as telegram_router
app.include_router(telegram_router)

# ─── In-memory job store ──────────────────────────────────────────────────────
# { job_id: { status, started_at, finished_at, stats, error } }
# Fine for a single-instance HF Space; swap for Redis if you scale out.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

ALL_SCRAPERS = ["github", "internshala", "indeed", "naukri", "unstop", "freshersworld", "letsintern"]
DEFAULT_MAX  = 300


# ─── Request / Response models ────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    """
    scrapers: list of scraper IDs, or list of {id: max} dicts to override per-scraper limits.

    Examples:
        {"scrapers": ["github", "internshala"]}
        {"scrapers": [{"github": 50}, {"internshala": 200}, "naukri"]}
        {"scrapers": [{"github": 100}], "skip_scrape": false}
    """
    scrapers:    list[str | dict[str, int]] = ALL_SCRAPERS
    skip_scrape: bool                       = False


class ProcessRequest(BaseModel):
    """One internship or a small batch, from one source (default "ifind")."""
    source:      str                       = "ifind"
    internship:  Optional[dict]            = None
    internships: Optional[list[dict]]      = None


MAX_PROCESS_BATCH = 50


class JobStatus(BaseModel):
    job_id:      str
    status:      str                   # "running" | "done" | "failed"
    started_at:  str
    finished_at: Optional[str] = None
    stats:       Optional[dict] = None
    error:       Optional[str]  = None


# ─── Background worker ────────────────────────────────────────────────────────

def _run_job(job_id: str, scrapers: list[str], skip_scrape: bool, max_values: dict[str, int]):
    """Runs in a daemon thread. Updates _jobs[job_id] when done."""
    log.info("[job:%s] Starting — scrapers=%s max_values=%s skip_scrape=%s",
             job_id, scrapers, max_values, skip_scrape)

    try:
        from script import main as run_pipeline
        def _progress(partial: dict):
            with _jobs_lock:
                _jobs[job_id]["stats"] = partial   # live totals after each scraper finishes

        stats = run_pipeline(selected_ids=scrapers, skip_scrape=skip_scrape, max_values=max_values, on_progress=_progress, job_id=job_id)

        with _jobs_lock:
            _jobs[job_id].update({
                "status":      "done",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "stats":       stats,
                "error":       None,
            })
        log.info("[job:%s] Done — %s", job_id, stats)

    except Exception as e:
        log.exception("[job:%s] Failed: %s", job_id, e)
        with _jobs_lock:
            _jobs[job_id].update({
                "status":      "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "stats":       None,
                "error":       str(e),
            })


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

# just to change git status
@app.post("/scrape", status_code=202)
def start_scrape(req: ScrapeRequest):
    """
    Kick off a background scrape job.

    scrapers field accepts mixed format:
      - plain string:  "github"              → uses DEFAULT_MAX (300)
      - dict:          {"github": 50}        → overrides max for that scraper

    Example body:
      {
        "scrapers": [{"github": 50}, {"internshala": 200}, "naukri"],
        "skip_scrape": false
      }
    """
    # Parse scrapers field into (id_list, max_values_dict)
    scraper_ids: list[str] = []
    max_values:  dict[str, int] = {}

    for entry in req.scrapers:
        if isinstance(entry, str):
            scraper_ids.append(entry)
        elif isinstance(entry, dict):
            for scraper_id, max_val in entry.items():
                scraper_ids.append(scraper_id)
                if isinstance(max_val, int) and max_val > 0:
                    max_values[scraper_id] = max_val
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Max value for '{scraper_id}' must be a positive integer, got: {max_val}",
                    )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Each entry in 'scrapers' must be a string or {{id: max}} dict, got: {entry}",
            )

    invalid = [s for s in scraper_ids if s not in ALL_SCRAPERS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scrapers: {invalid}. Valid options: {ALL_SCRAPERS}",
        )

    job_id     = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    with _jobs_lock:
        _jobs[job_id] = {
            "status":      "running",
            "started_at":  started_at,
            "finished_at": None,
            "stats":       None,
            "error":       None,
        }

    t = threading.Thread(
        target=_run_job,
        args=(job_id, scraper_ids, req.skip_scrape, max_values),
        daemon=True,
    )
    t.start()

    return {
        "job_id":     job_id,
        "status":     "running",
        "scrapers":   scraper_ids,
        "max_values": {s: max_values.get(s, DEFAULT_MAX) for s in scraper_ids},
        "message":    (
            f"Scrape job started for: {', '.join(scraper_ids)}. "
            f"Poll GET /scrape/{job_id} for status."
        ),
        "started_at": started_at,
    }


@app.get("/scrape/{job_id}", response_model=JobStatus)
def get_job_status(job_id: str):
    """Poll the status of a scrape job."""
    with _jobs_lock:
        job = _jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    return JobStatus(job_id=job_id, **job)


@app.get("/scrape")
def list_jobs():
    """List all jobs (most recent first)."""
    with _jobs_lock:
        jobs = [
            {"job_id": jid, **info}
            for jid, info in _jobs.items()
        ]
    jobs.sort(key=lambda j: j.get("started_at", ""), reverse=True)
    return {"jobs": jobs, "total": len(jobs)}


@app.post("/process")
def process_internships(req: ProcessRequest, x_api_key: Optional[str] = Header(default=None)):
    """Scam-check one internship or a batch (platform path); returns verdicts, records them on the ifind document and sends auto-approved posts to the vectoriser"""
    expected = os.environ.get("INTERNSCRAPER_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")

    from pipeline import INTERNSHIP_SOURCES, evaluate_internships

    if req.source not in INTERNSHIP_SOURCES:
        raise HTTPException(status_code=400, detail=f"Unknown source '{req.source}'. Valid: {list(INTERNSHIP_SOURCES)}")
    items = ([req.internship] if req.internship else []) + (req.internships or [])
    if not items:
        raise HTTPException(status_code=400, detail="Send 'internship' (one) or 'internships' (a list).")
    if len(items) > MAX_PROCESS_BATCH:
        raise HTTPException(status_code=400, detail=f"At most {MAX_PROCESS_BATCH} internships per call, got {len(items)}.")

    try:
        result = evaluate_internships(items, req.source)
        if req.source == "ifind":
            from pipeline import apply_platform_verdicts
            result = apply_platform_verdicts(result)
        return result
    except Exception as exc:
        log.exception("[process] failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ─── Display Routes on Startup ────────────────────────────────────────────────

ENDPOINT_DESCRIPTIONS = {
    "/health": "Liveness health check to verify service status",
    ("/scrape", "POST"): "Starts a background scraping job for selected internship sources",
    ("/process", "POST"): "Scam-checks one internship or a batch (platform path); records the verdict and triggers the vectoriser",
    ("/scrape", "GET"): "Lists all recent scraping jobs and their overall execution status",
    "/scrape/{job_id}": "Polls status, progress, and stats of a specific scraping job",
    "/docs": "Interactive Swagger UI documentation and API testing dashboard",
    "/redoc": "ReDoc API reference documentation page",
    "/openapi.json": "Raw OpenAPI JSON schema definition of the API",
    "/docs/oauth2-redirect": "OAuth2 authentication redirect callback for Swagger UI",
}

@app.on_event("startup")
def print_routes():
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            methods = ",".join(sorted(route.methods))
            path = route.path

            primary_method = "POST" if "POST" in route.methods else "GET"
            desc = (
                ENDPOINT_DESCRIPTIONS.get((path, primary_method))
                or ENDPOINT_DESCRIPTIONS.get(path)
            )
            if not desc and getattr(route, "endpoint", None) and route.endpoint.__doc__:
                desc = route.endpoint.__doc__.strip().splitlines()[0].strip()
            desc = desc or "No description available"

            logging.getLogger("routes").info(desc, extra={"f": {"method": methods, "path": path}})
    log.ok("internScraper API ready", extra={"f": {"scrapers": ",".join(ALL_SCRAPERS)}})




# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))   # HF Spaces default port
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

