# iFind Scraper API

FastAPI service that scrapes internship listings from 7 sources, runs them through the
moderation pipeline (validation → dedup → scam detection) and stores them in MongoDB
staging collection **`internships.mod-unvectorised`**.

Deployed on **Render** as a Docker web service (`render.yaml`). The Dockerfile installs Chrome for the
Selenium scrapers and listens on Render's `$PORT`.

## Pipeline

```
POST /scrape → for each scraper, one after another:
    scrape → validate required fields → SHA-256 fingerprint dedup (vs staging AND live `internships`)
          → scam_detector → insert into  internships.mod-unvectorised
          → POST {VECTORISER_URL}/vectorize-hnsw   (best effort; vectorizes + graph-indexes auto-approved items,
                                                     then the vectoriser re-runs every student through the graph)
```

Results are saved **per scraper, as soon as that scraper finishes** (not at the end of the whole run), so a
crash, restart or out-of-memory kill loses at most the scraper that was running. Listings are scored in
one batch per scraper; duplicate and peer-group checks in the detector work within that batch.

Scam-detector decision → `moderation.status` on the staged document:

| decision | score band | `moderation.status` | what happens next |
|----------|-------------|---------------------|-------------------|
| `clear`  | < 30        | `auto_approved`     | vectorizer publishes it to `internships` |
| `review` | 30 – 70     | `pending_review`    | waits in staging for a moderator |
| `block`  | ≥ 70        | `auto_rejected`     | stays in staging |

If the detector itself fails, **every** listing is routed to `pending_review` (never auto-approved).

Each staged document contains the listing fields plus `moderation` (`status`, `score`,
`flags`, `source`, `reviewedBy`, `reviewedAt`, `rejectionReason`, `scamDetails`). It carries
no vectors; those are added by the vectorizer when the listing is published.

**Source.** Every listing carries `source`: the scraper id (`github`, `internshala`, `indeed`, `naukri`,
`unstop`, `freshersworld`, `letsintern`) or `ifind` for internships employers post on our own platform.
`moderation.source` says how it arrived (`web_scraping` or `employer`). Older documents still say
`source: "web_scraping"`; `python backfill_source.py` (dry run) / `--apply` replaces it with the real scraper id.

**Platform path.** Employer-posted internships use the same validation and scam detection through
`POST /process`. The document belongs to ifind (`internships.this-platform`), so nothing is staged here: the verdict
goes back to the caller, and for an existing document that is still `pending_review` it is also written onto it.
Auto-approved posts are then sent to the vectoriser (`POST {VECTORISER_URL}/vectorize-platform`), which vectorizes them
in place, adds them to the graph and re-runs every student through it.
The full flow for both paths is drawn in [`pipeline-diagram.md`](pipeline-diagram.md).

## Run log (`ifind.scrape.runs`)

Every run writes one document, updated after each scraper (a crash keeps what finished). Top level:
`jobId`, `trigger` (`scrape`/`checkpoint`), `status` (`running`/`done`/`failed`), `startedAt`, `finishedAt`,
`durationSeconds`, `requested` (`{scraper: max}`), `totals`, `error`, `sources[]`.

Each `sources[]` entry (one per scraper): `scraped`, `saved`, `duplicate`, `rejected`
(+ `rejectedMissingFields`, `rejectedBadUrl`), `errors`, `scrapeSeconds`, `pipelineSeconds`, `detectorSeconds`,
`scraperError` / `pipelineError` / `detectorFailed` / `emptyResult` (reliability), `statusCounts`
(auto_approved / pending_review / auto_rejected), `decisionCounts`, `scoreHistogram` (ten buckets `0-10` … `90-100`),
`lowConfidence`, `hardDisqualified`, `topTriggeredRules`, `uniqueCompanies`, `stipend` (paid/unpaid/performanceBased),
`remote`, `noSkills`, `noDeadline`. Averages and rates are not stored; compute them on read.

## Endpoints

Base URL: `https://<service>.onrender.com` · Interactive docs: `/docs` (Swagger), `/redoc`, `/openapi.json`

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Liveness check → `{"status": "ok"}` |
| `POST` | `/scrape` | Start a background scrape job → **202** with `job_id` |
| `GET`  | `/scrape/{job_id}` | Poll a job's status and stats |
| `GET`  | `/scrape` | List all jobs, newest first |
| `POST` | `/process` | Scam-check one internship or a batch (platform path); returns verdicts, records them on the ifind document and sends auto-approved posts to the vectoriser |

There is no auth on these endpoints. Anyone with the URL can start a scrape; keep the service
private or put a gateway in front of it.

### `POST /scrape`

Scraping takes minutes (HF proxies time out at ~60 s), so the job runs in a background thread
and the call returns immediately.

Request body (all fields optional):

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `scrapers` | `list[string \| {id: max}]` | all 7 | Plain id uses the default cap of **300** items; `{id: n}` overrides the cap for that scraper (`n` must be a positive integer). |
| `skip_scrape` | `bool` | `false` | `true` skips scraping and re-pushes `checkpoint_internships.json` through the pipeline. The checkpoint lives in the container and is lost on restart. |

Valid scraper ids: `github`, `internshala`, `indeed`, `naukri`, `unstop`, `freshersworld`, `letsintern`.

```bash
# everything, default caps
curl -X POST https://<service>.onrender.com/scrape -H "Content-Type: application/json" -d '{}'

# per-scraper caps, mixed with plain ids
curl -X POST https://<service>.onrender.com/scrape -H "Content-Type: application/json" \
  -d '{"scrapers": [{"github": 50}, {"internshala": 200}, "naukri"]}'
```

Response `202`:

```json
{
  "job_id": "7c1f…",
  "status": "running",
  "scrapers": ["github", "internshala", "naukri"],
  "max_values": {"github": 50, "internshala": 200, "naukri": 300},
  "message": "Scrape job started for: github, internshala, naukri. Poll GET /scrape/7c1f… for status.",
  "started_at": "2026-10-05T14:00:00+00:00"
}
```

Errors: `400` for an unknown scraper id, a non-positive max, or an entry that is neither a string nor `{id: max}`.

### `GET /scrape/{job_id}`

```json
{
  "job_id": "7c1f…",
  "status": "running | done | failed",
  "started_at": "…",
  "finished_at": "… | null",
  "stats": {"saved": 120, "duplicate": 30, "rejected": 12, "errors": 0},
  "error": null
}
```

`stats` is updated **live after each scraper finishes** (so you can watch `saved` grow while the job is still
`running`) and is final when `done`. It also includes `scraped` (items returned by scrapers). Fields: `saved` = inserted into staging, `duplicate` = fingerprint already in
staging or live, `rejected` = missing name/company/link/summary or non-http link, `errors` = unexpected
per-item failures. `404` if the job id is unknown.

### `GET /scrape`

```json
{"jobs": [{"job_id": "…", "status": "done", "started_at": "…", "finished_at": "…", "stats": {…}, "error": null}], "total": 1}
```

### `POST /process`

For internships posted on our own platform. Synchronous, 1 to 50 items per call. For `source: "ifind"` each item should
carry the ifind document's `id` and `companyVerified` (boolean): a post is auto-approved only when `companyVerified` is
exactly `true`, otherwise a clear listing comes back `pending_review` with the flag `unverified_company`.

```json
{ "source": "ifind", "internship": { "id": "abc", "name": "...", "company": "...", "applyLink": "https://...", "summary": "..." } }
```
or `"internships": [ {...}, {...} ]`. `source` defaults to `ifind` and must be one of the sources above.
Item fields are the same loose shape the scrapers produce (`name`/`title`, `applyLink`/`apply_link`,
`summary`/`description`, `city`/`location` (or `address`: a list of `{city, state, country}`), `skills`, `stipend`, `duration`, `deadline_date`, ...).

Response `200`:

```json
{
  "source": "ifind", "count": 1, "detectorFailed": false,
  "summary": { "auto_approved": 1, "pending_review": 0, "auto_rejected": 0, "rejected": 0, "errors": 0 },
  "results": [ {
    "index": 0, "id": "abc", "outcome": "scored", "reason": null,
    "moderationStatus": "auto_approved", "score": 5.4, "decision": "clear", "confidence": 0.8,
    "flags": [], "rejectionReason": null, "fingerprint": "...", "scamDetails": { "...": "..." }
  } ]
}
```

For `source: "ifind"`, when `id` is a document in `internships.this-platform` that is still `pending_review`, the
verdict (`moderation.status`, score, flags, rejection reason, scam details) is written onto it (`persisted: true`) and, if
auto-approved, `{VECTORISER_URL}/vectorize-platform` is called in the background (`vectorizerTriggered: true`). A document a
moderator already decided is never overwritten.

`results[i]` matches the i-th input. `outcome` is `scored`, `rejected` (`reason`: `missing_required_fields` or
`invalid_url`) or `error`. No de-duplication is done here (employers may repost). If the detector fails, every
item comes back `pending_review`. `400` for an unknown source, no items, or more than 50; `401` when
`INTERNSCRAPER_API_KEY` is set and the `x-api-key` header does not match.

### Notes

- Jobs are stored **in memory** (single instance, `--workers 1`). They disappear on restart or when
  the service sleeps; staged documents in MongoDB are unaffected.
- Concurrent `POST /scrape` calls are not blocked. Avoid starting a second job while one is running.

## Addresses

A listing stores its locations as `address: [{city, state, country}, ...]` (no flat `city`/`state`/`country`).
Scrapers still produce one flat `city`; the pipeline wraps it. The dedup fingerprint uses the **first** address's city.
Documents from before this change are converted once with `python migrate_address.py` (dry run) / `--apply`;
run it before deploying code that reads `address`.

## Telegram bot (`scrapers/telegram_bot/`)

Forward internship posts (e.g. from WhatsApp) to the bot. For each post it opens the apply link, reads the details
from the page and runs them through the same pipeline as the scrapers with `source: "telegram"`
(`moderation.source: "community_bot"`).

```
Telegram → POST /telegram/webhook → split into posts → fetch the link (requests, Selenium if JS-rendered)
         → Cohere extraction (page beats message) → push_to_pipeline → one reply per post
```

- A pasted WhatsApp chat export is split on its `[date, time] sender:` headers; a plain message is one post.
- One internship per role in a post; every location goes into that internship's `address[]`.
- Not saved (and the bot says why): page can't be loaded / login wall / closed, no company or role found, no link or email.
- A post with only an email address is saved with `applyLink: "mailto:<address>"` (allowed for this source only). The scam
  detector scores the email's domain; a free-mail address (gmail, ...) adds `free_email_domain` and is never auto-approved.
- Only user ids in `TELEGRAM_ALLOWED_USER_IDS` are served.
- Register the webhook once after deploying: `python -m scrapers.telegram_bot.bot set-webhook https://<host>/telegram/webhook`.
- Tests: `pytest scrapers/telegram_bot/tests`. `scrapers/telegram_bot/examples.txt` (local sample messages) is gitignored.

## Environment secrets

Set in the Render dashboard (**Environment**); `sync: false` entries in `render.yaml` are prompted for on first deploy:

| Secret | Required | Description |
|--------|----------|-------------|
| `MONGODB_URI` | yes | MongoDB Atlas connection string (database `ifind`) |
| `COHERE_API_KEY` | for GitHub / Indeed / Internshala | AI enrichment |
| `MAIN_URL` | optional | The main project (ifind): browser origin(s) allowed by CORS, comma-separated (e.g. `https://ifind.example.com,http://localhost:3000`). Nothing is allowed by default. The old name `MAIN_SERVER` is still read when this is unset. |
| `FIRST_PARTY_DOMAINS` | for `/process` | iFind's own domains, comma-separated (e.g. `ifind.app`; `localhost:3000` in dev). Employer listings link to iFind itself; without this the scam detector treats that link as an off-platform domain and scores every employer post into review. Exact domain or real subdomain only. |
| `INTERNSCRAPER_API_KEY` | optional | If set, `POST /process` requires it in the `x-api-key` header. |
| `TELEGRAM_BOT_TOKEN` | for the Telegram bot | Token from @BotFather. |
| `TELEGRAM_WEBHOOK_SECRET` | for the Telegram bot | Any random string. Telegram echoes it in a header; `POST /telegram/webhook` rejects requests without it (503 while unset). |
| `TELEGRAM_ALLOWED_USER_IDS` | for the Telegram bot | Comma-separated Telegram user ids allowed to submit posts. Everyone else is ignored. |
| `VECTORISER_URL` | recommended | Base URL of the vectoriser service (`tf-idfs+berts/vectoriser`). After a push, `POST {VECTORISER_URL}/vectorize-hnsw` vectorizes and graph-indexes auto-approved listings, and the vectoriser re-runs every student through the graph. `/process` calls `/vectorize-platform` the same way for auto-approved employer posts. Unset → they wait until the vectorizer is triggered some other way. |

## Deploying to Render

1. Push the repo to GitHub. In Render: **New + → Blueprint**, select the repo; it reads `render.yaml`
   (or create a **Web Service** manually: Runtime *Docker*, health check path `/health`).
2. Fill in the env vars above. Render sets `PORT` itself.
3. After the build, check `GET /health`, then smoke test `POST /scrape` with `{"scrapers": [{"github": 5}]}`,
   poll the job, and confirm documents appear in `internships.mod-unvectorised`.

Things to know about Render:

- **Memory:** Chrome plus the scam-detector stack (pandas, scikit-learn) needs roughly 2 GB. `render.yaml`
  uses the `standard` plan; the free and starter plans (512 MB) are likely to be OOM-killed during a scrape.
- **Free plan spin-down:** free services sleep after ~15 minutes without traffic, and a sleeping service
  also kills a running scrape thread. Use a paid plan if scrapes must complete unattended.
- **Disk is ephemeral:** the checkpoint file, scam-detector reputation/baseline files and the in-memory job list
  reset on every deploy or restart. Staged documents in MongoDB are unaffected.
- **Region:** pick the one closest to your MongoDB Atlas cluster, and allow Render's outbound IPs in
  Atlas Network Access (or `0.0.0.0/0`).
