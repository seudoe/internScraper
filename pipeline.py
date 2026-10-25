"""
pipeline.py — Moderation pipeline integrated with scam_detector

Validates, deduplicates, runs scam detection and normalizes internships to the
canonical Internship schema defined in ifind/types/internship.ts.

Two entry points share the same stages (see pipeline-diagram.md):
  push_to_pipeline()      scraper path: stores the results in 'internships.mod-unvectorised'
  evaluate_internships()  platform path (source "ifind"): returns the verdicts, stores nothing
"""

import os
import re
import hashlib
import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Literal, TypedDict, Any, Dict

from pymongo.collection import Collection

log = logging.getLogger("pipeline")


# ─── Python TypedDict definitions (matching ifind/types/internship.ts) ────────

class StipendDict(TypedDict, total=False):
    type: Literal["paid", "unpaid", "performance-based"]
    amount: Optional[float]
    currency: Optional[str]
    period: Optional[Literal["monthly", "weekly", "lump-sum"]]


class DurationDict(TypedDict, total=False):
    value: int
    unit: Literal["weeks", "months"]


class ExperienceRequiredDict(TypedDict, total=False):
    min: Optional[int]
    max: Optional[int]
    unit: Literal["months", "years"]


class LinkVerificationDict(TypedDict, total=False):
    reachable: Optional[bool]
    statusCode: Optional[int]
    redirectedTo: Optional[str]
    isScamSuspected: Optional[bool]
    isExpired: Optional[bool]
    scamSignals: List[str]
    checkedAt: Optional[str]
    nextCheckAt: Optional[str]


class RiskBreakdownDict(TypedDict, total=False):
    textRisk: Optional[float]
    companyRisk: Optional[float]
    urlRisk: Optional[float]
    stipendRisk: Optional[float]
    anomalyScore: Optional[float]


class ScamDetailsDict(TypedDict, total=False):
    score: float
    decision: Literal["clear", "review", "block"]
    confidence: float
    explanationSummary: str
    scamFlags: List[str]
    evaluatedAt: Optional[str]
    riskBreakdown: Optional[RiskBreakdownDict]


class ModerationDict(TypedDict, total=False):
    status: Literal["auto_approved", "pending_review", "auto_rejected", "manually_approved", "manually_rejected"]
    score: Optional[float]
    flags: List[str]
    source: Literal["web_scraping", "api", "user_contributed", "email_parsing", "rss", "community_bot", "manual", "employer"]
    reviewedBy: Optional[str]
    reviewedAt: Optional[str]
    rejectionReason: Optional[str]
    scamDetails: Optional[ScamDetailsDict]


InternshipSource = Literal["github", "internshala", "indeed", "naukri", "unstop", "freshersworld", "letsintern", "telegram", "ifind"]


class AddressDict(TypedDict, total=False):
    city: Optional[str]
    state: Optional[str]
    country: Optional[str]


class InternshipDict(TypedDict, total=False):
    _id: Optional[str]
    _ix: int   # position in the input batch (internal; removed before storing)
    name: str
    company: str
    applyLink: str
    datePublished: str
    deadlineDate: Optional[str]
    address: List["AddressDict"]
    isRemote: bool
    stipend: StipendDict
    duration: DurationDict
    skills: List[str]
    degree: Optional[List[str]]
    field: Optional[List[str]]
    experienceRequired: Optional[ExperienceRequiredDict]
    openings: Optional[int]
    summary: str
    responsibilities: Optional[List[str]]
    perks: Optional[List[str]]
    tags: Optional[List[str]]
    source: InternshipSource
    isActive: bool
    bannedByCompany: Optional[bool]   # platform posts only: pulled because the company was banned (ifind companies.restriction.status)
    fingerprint: Optional[str]
    linkVerification: Optional[LinkVerificationDict]
    moderation: Optional[ModerationDict]
    createdAt: str
    updatedAt: str


# ─── Fingerprint ──────────────────────────────────────────────────────────────

def generate_fingerprint(company: str, name: str, city: str) -> str:
    raw = f"{company.lower().strip()}:{name.lower().strip()}:{(city or 'remote').lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── Normalizers ──────────────────────────────────────────────────────────────

def normalize_duration(raw) -> DurationDict:
    """Accept a dict with value+unit, a duration_string, or fall back to default."""
    if isinstance(raw, dict) and raw.get("value") and raw.get("unit"):
        unit = "weeks" if "week" in str(raw.get("unit")).lower() else "months"
        return {"value": int(raw["value"]), "unit": unit}

    if isinstance(raw, str) and raw:
        m = re.search(r"(\d+)\s*(week|month)", raw, re.IGNORECASE)
        if m:
            unit = "weeks" if "week" in m.group(2).lower() else "months"
            return {"value": int(m.group(1)), "unit": unit}

    return {"value": 3, "unit": "months"}


def normalize_stipend(raw) -> StipendDict:
    """Accept a structured dict or a plain string like '₹ 12,000 /month'."""
    if isinstance(raw, dict) and raw.get("type"):
        stype = str(raw.get("type")).lower().replace("_", "-")
        if stype not in ("paid", "unpaid", "performance-based"):
            stype = "paid" if raw.get("amount") else "unpaid"
        return {
            "type": stype,
            "amount": float(raw.get("amount")) if raw.get("amount") is not None else None,
            "currency": str(raw.get("currency") or "INR"),
            "period": raw.get("period") or None,
        }

    if isinstance(raw, str) and raw and raw.lower() not in ("n/a", "unpaid", "not disclosed"):
        text = raw.strip()

        if re.search(r"unpaid|volunteer|no stipend", text, re.IGNORECASE):
            return {"type": "unpaid", "amount": None, "currency": "INR", "period": None}

        if re.search(r"performance|incentive", text, re.IGNORECASE):
            return {"type": "performance-based", "amount": None, "currency": "INR", "period": None}

        currency = "INR"
        if "$" in text or "USD" in text:
            currency = "USD"
        elif "£" in text or "GBP" in text:
            currency = "GBP"
        elif "€" in text or "EUR" in text:
            currency = "EUR"

        clean = text.replace(",", "")
        amount_match = re.search(r"(\d+)", clean)
        amount = float(amount_match.group(1)) if amount_match else None

        period = None
        if re.search(r"/month|per month|monthly", text, re.IGNORECASE):
            period = "monthly"
        elif re.search(r"/week|per week|weekly", text, re.IGNORECASE):
            period = "weekly"
        elif re.search(r"lump.?sum|one.?time|total", text, re.IGNORECASE):
            period = "lump-sum"

        return {
            "type": "paid" if amount else "unpaid",
            "amount": amount,
            "currency": currency,
            "period": period,
        }

    return {"type": "unpaid", "amount": None, "currency": "INR", "period": None}


def normalize_location(item: dict) -> tuple[str, str, bool]:
    """Return (city, country, is_remote) from various scraper field layouts."""
    first_addr = next((a for a in (item.get("address") or []) if isinstance(a, dict) and a.get("city")), {}) if isinstance(item.get("address"), list) else {}
    city = (
        first_addr.get("city")
        or item.get("city")
        or item.get("location")
        or "remote"
    )
    if isinstance(city, str):
        city = city.strip()
    else:
        city = "remote"

    is_remote = bool(re.search(r"remote|wfh|work from home|online", city, re.IGNORECASE)) or item.get("isRemote") is True

    country = first_addr.get("country") or item.get("country") or ""
    if not country:
        if re.search(r"remote|wfh|online", city, re.IGNORECASE):
            country = ""
        elif re.search(
            r"mumbai|delhi|bangalore|bengaluru|hyderabad|pune|chennai|kolkata|"
            r"noida|gurgaon|india|jaipur|lucknow|chandigarh|indore|bhopal|kochi",
            city, re.IGNORECASE
        ):
            country = "india"
        else:
            country = ""

    return city, country, is_remote


def normalize_address(item: dict, city: str, country: str, is_remote: bool) -> list:
    """
    Return the `address` list: [{city, state, country}, ...].

    An item that already carries `address` (list of dicts, e.g. ifind employer posts or the Telegram bot)
    keeps every entry that has a city or state. Otherwise one entry is built from the flat
    city/state/country fields scrapers use (`city` and `country` come from normalize_location).
    Remote-only listings with no real location get an empty list.
    """
    raw = item.get("address")
    if isinstance(raw, list):
        out = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            entry = {k: (str(a.get(k)).strip() or None) if a.get(k) else None for k in ("city", "state", "country")}
            if entry["city"] or entry["state"]:
                out.append(entry)
        return out

    if is_remote and not item.get("state"):
        return []
    state = item.get("state")
    entry = {
        "city": city or None,
        "state": str(state).strip() or None if state else None,
        "country": country or None,
    }
    return [entry] if entry["city"] or entry["state"] else []


def normalize_skills(raw) -> list:
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if s]
    return []


def normalize_name(item: dict) -> str:
    """Scrapers use 'name' or 'title'."""
    return (item.get("name") or item.get("title") or "").strip()


def normalize_apply_link(item: dict) -> str:
    return (item.get("apply_link") or item.get("applyLink") or item.get("link") or "").strip()


def normalize_summary(item: dict) -> str:
    """Scrapers use 'summary', 'description', or 'responsibilities'."""
    s = item.get("summary") or item.get("description") or ""
    if not s and isinstance(item.get("responsibilities"), list):
        s = " ".join(item["responsibilities"][:3])
    return str(s).strip()


def normalize_link_verification(item: dict) -> LinkVerificationDict:
    """Preserve existing linkVerification / linkVerified or build non-null defaults."""
    raw = item.get("linkVerification") or item.get("linkVerified")
    now_iso = datetime.now(timezone.utc).isoformat()
    next_check_iso = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    if isinstance(raw, dict):
        return {
            "reachable": raw.get("reachable") if raw.get("reachable") is not None else True,
            "statusCode": raw.get("statusCode") if raw.get("statusCode") is not None else 200,
            "redirectedTo": raw.get("redirectedTo") or None,
            "isScamSuspected": raw.get("isScamSuspected") if raw.get("isScamSuspected") is not None else False,
            "isExpired": raw.get("isExpired") if raw.get("isExpired") is not None else False,
            "scamSignals": raw.get("scamSignals") if isinstance(raw.get("scamSignals"), list) else [],
            "checkedAt": raw.get("checkedAt") or now_iso,
            "nextCheckAt": raw.get("nextCheckAt") or next_check_iso,
        }

    return {
        "reachable": True,
        "statusCode": 200,
        "redirectedTo": None,
        "isScamSuspected": False,
        "isExpired": False,
        "scamSignals": [],
        "checkedAt": now_iso,
        "nextCheckAt": next_check_iso,
    }


def score_internship(name: str, company: str, apply_link: str, summary: str, skills: list) -> int:
    """
    Simplified scoring check. Max possible = 50.
    """
    score = 0
    if name and company and apply_link and summary:
        score += 25
    if len(skills) >= 1:
        score += 10
    if len(summary) >= 80:
        score += 10
    score += 5
    return score


# Top-level keys scam_detector.process_records adds; the useful parts are
# folded into doc["moderation"]["scamDetails"], so they must not leak into staging.
_DETECTOR_EXTRA_KEYS = (
    "scam_score", "decision", "confidence", "explanation_summary", "confidence_level",
    "triggered_rules", "top_contributing_features", "risk_breakdown",
    "hard_disqualifying_forced", "low_confidence_forced_review",
    "shared_infrastructure", "duplicate_cluster_network_size",
    "_ix", "city", "_freeMail",
)


_HIST_KEYS = [f"{i * 10}-{i * 10 + 10}" for i in range(10)]


def _new_detail() -> dict:
    return {
        "validated": 0,
        "rejectedMissingFields": 0,
        "rejectedBadUrl": 0,
        "detectorFailed": False,
        "detectorError": None,
        "detectorSeconds": None,
        "statusCounts": {"auto_approved": 0, "pending_review": 0, "auto_rejected": 0},
        "decisionCounts": {"clear": 0, "review": 0, "block": 0},
        "scoreHistogram": {k: 0 for k in _HIST_KEYS},   # scam-score buckets; averages are computed on read
        "lowConfidence": 0,
        "hardDisqualified": 0,
        "stipend": {"paid": 0, "unpaid": 0, "performanceBased": 0},
        "remote": 0,
        "noSkills": 0,
        "noDeadline": 0,
        "topTriggeredRules": {},
        "uniqueCompanies": 0,
    }


def _record_saved(detail, doc, meta, flag_counts, companies) -> None:
    detail["statusCounts"][meta["status"]] += 1
    detail["decisionCounts"][meta["decision"] if meta["decision"] in detail["decisionCounts"] else "review"] += 1
    detail["scoreHistogram"][_HIST_KEYS[min(max(int(meta["score"] // 10), 0), 9)]] += 1
    if meta["lowConfidence"]:
        detail["lowConfidence"] += 1
    if meta["hardDisqualified"]:
        detail["hardDisqualified"] += 1
    stype = (doc.get("stipend") or {}).get("type")
    key = {"paid": "paid", "unpaid": "unpaid", "performance-based": "performanceBased"}.get(stype)
    if key:
        detail["stipend"][key] += 1
    detail["remote"] += bool(doc.get("isRemote"))
    detail["noSkills"] += not doc.get("skills")
    detail["noDeadline"] += not doc.get("deadlineDate")
    flag_counts.update(meta["rules"])
    companies.add(str(doc.get("company", "")).lower())


# ─── Sources ──────────────────────────────────────────────────────────────────

# Where an internship came from: one of the scrapers, or "ifind" (posted by an employer on our own platform).
INTERNSHIP_SOURCES = ("github", "internshala", "indeed", "naukri", "unstop", "freshersworld", "letsintern", "telegram", "ifind")

STAGING_COLLECTION = "internships.mod-unvectorised"
LIVE_COLLECTION = "internships"
PLATFORM_COLLECTION = "internships.this-platform"   # employer-posted (source "ifind"); written by ifind, never staged here


def _check_source(source: str) -> str:
    if source not in INTERNSHIP_SOURCES:
        raise ValueError(f"Unknown source '{source}'. Expected one of: {', '.join(INTERNSHIP_SOURCES)}")
    return source


def _moderation_source(source: str) -> str:
    """moderation.source says how the listing arrived, not which site it came from."""
    if source == "ifind":
        return "employer"
    return "community_bot" if source == "telegram" else "web_scraping"


# ─── Pipeline stages (shared by push_to_pipeline and evaluate_internships) ────

def _prepare_candidates(items: list, source: str, is_duplicate, stats: dict, detail: dict, outcomes: dict) -> list:
    """
    Stage 1: validate, de-duplicate and normalize raw items into canonical candidate docs.

    Each candidate carries `_ix` (its position in `items`) so a result can be matched back to its input.
    `is_duplicate(fingerprint) -> bool`; None skips de-duplication (employers may repost).
    `outcomes[ix]` records why an item produced no candidate.
    """
    candidates: list[dict] = []

    for ix, item in enumerate(items):
        try:
            name       = normalize_name(item)
            company    = (item.get("company") or "").strip()
            apply_link = normalize_apply_link(item)
            summary    = normalize_summary(item)
            city, country, is_remote = normalize_location(item)
            address    = normalize_address(item, city, country, is_remote)
            skills     = normalize_skills(item.get("skills"))

            # ── Basic validation ──────────────────────────────────────────
            if not name or not company or not apply_link or not summary:
                log.debug("  ⚠  Skipping '%s' — missing required fields", name or "unnamed")
                stats["rejected"] += 1
                detail["rejectedMissingFields"] += 1
                outcomes[ix] = {"outcome": "rejected", "reason": "missing_required_fields"}
                continue

            # Telegram posts that are applied for by email carry a mailto: link; every other source needs a URL
            if not (apply_link.startswith("http") or (source == "telegram" and apply_link.startswith("mailto:"))):
                log.debug("  ⚠  Skipping '%s' — invalid URL: %s", name, apply_link)
                stats["rejected"] += 1
                detail["rejectedBadUrl"] += 1
                outcomes[ix] = {"outcome": "rejected", "reason": "invalid_url"}
                continue

            # ── Deduplication ─────────────────────────────────────────────
            # Fingerprint uses the first address's city (same value the flat `city` field used to hold)
            fingerprint = generate_fingerprint(company, name, (address[0].get("city") if address else None) or city)
            if is_duplicate and is_duplicate(fingerprint):
                log.debug("  ⏭  Duplicate: '%s' @ %s", name, company)
                stats["duplicate"] += 1
                outcomes[ix] = {"outcome": "duplicate", "reason": "duplicate", "fingerprint": fingerprint}
                continue

            # ── Normalize sub-fields ──────────────────────────────────────
            stipend  = normalize_stipend(item.get("stipend") or item.get("stipend_text") or "")
            duration = normalize_duration(item.get("duration") or item.get("duration_string") or "")

            deadline_date_str: Optional[str] = None
            if item.get("deadline_date"):
                try:
                    dt = datetime.fromisoformat(str(item["deadline_date"]))
                    deadline_date_str = dt.isoformat()
                except Exception:
                    deadline_date_str = str(item["deadline_date"])

            raw_degree = item.get("degree")
            degree = (
                raw_degree if isinstance(raw_degree, list)
                else [raw_degree] if raw_degree
                else None
            )
            raw_field = item.get("field")
            field = (
                raw_field if isinstance(raw_field, list)
                else [raw_field] if raw_field
                else None
            )

            now_iso = datetime.now(timezone.utc).isoformat()

            # Construct InternshipDict matching canonical TS schema
            candidate_doc: InternshipDict = {
                "_ix":                ix,
                "name":               name,
                "company":            company,
                "applyLink":          apply_link,
                "summary":            summary,
                "address":            address,
                "city":               city or None,   # detector input only; dropped by _assemble_moderation
                "isRemote":           is_remote,
                "skills":             skills,
                "degree":             degree,
                "field":              field,
                "responsibilities":   (
                    item["responsibilities"]
                    if isinstance(item.get("responsibilities"), list)
                    else None
                ),
                "perks":              item.get("perks") if isinstance(item.get("perks"), list) else None,
                "tags":               item.get("tags") if isinstance(item.get("tags"), list) else None,
                "openings":           item.get("openings") or None,
                "source":             source,
                "isActive":           True,
                "datePublished":      now_iso,
                "deadlineDate":       deadline_date_str,
                "stipend":            stipend,
                "duration":           duration,
                "experienceRequired": {"unit": "months"},
                "fingerprint":        fingerprint,
                "linkVerification":   normalize_link_verification(item),
                "createdAt":           now_iso,
                "updatedAt":           now_iso,
            }

            candidates.append(candidate_doc)

        except Exception as e:
            log.error("  💥 Error processing '%s': %s", item.get("name") or item.get("title", "?"), e)
            stats["errors"] += 1
            outcomes[ix] = {"outcome": "error", "reason": f"{type(e).__name__}: {e}"[:200]}

    return candidates


FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
    "rediffmail.com", "icloud.com", "proton.me", "protonmail.com", "aol.com", "zoho.com", "ymail.com",
})


def mailto_domain(link: str) -> str:
    """Domain of a mailto: link ("mailto:hr@foxo.club?subject=x" -> "foxo.club"), "" when it has none."""
    addr = link[len("mailto:"):].split("?")[0].strip()
    return addr.rsplit("@", 1)[1].lower() if "@" in addr else ""


def _hide_mailto(candidates: list) -> dict:
    """
    The URL features of the scam detector expect a web link. For a mailto: apply link, score the email's
    domain as https://<domain> instead (so it is still compared with the company name) and restore the
    mailto: afterwards. Returns {_ix: original link}.
    """
    originals: dict = {}
    for c in candidates:
        link = c.get("applyLink") or ""
        if link.startswith("mailto:"):
            originals[c["_ix"]] = link
            c["applyLink"] = f"https://{mailto_domain(link) or 'invalid.invalid'}"
    return originals


def _restore_mailto(docs: list, originals: dict) -> None:
    for d in docs:
        link = originals.get(d.get("_ix"))
        if link:
            d["applyLink"] = link
            if mailto_domain(link) in FREE_MAIL_DOMAINS:
                d["_freeMail"] = True


def _score_candidates(candidates: list, detail: dict) -> list:
    """Stage 2: scam_detector. Fails closed: if it breaks, nothing is auto-approved."""
    originals = _hide_mailto(candidates)
    try:
        scored = _run_detector(candidates, detail)
    finally:
        _restore_mailto(candidates, originals)
    _restore_mailto(scored, originals)
    return scored


def _run_detector(candidates: list, detail: dict) -> list:
    try:
        from scam_detector.pipeline import process_records
        t0 = time.monotonic()
        scored = process_records(candidates)
        detail["detectorSeconds"] = round(time.monotonic() - t0, 2)
        # Keep results matched to inputs even if the detector rebuilt the dicts
        if len(scored) == len(candidates):
            for s, c in zip(scored, candidates):
                s.setdefault("_ix", c["_ix"])
        return scored
    except Exception as exc:
        detail["detectorFailed"] = True
        detail["detectorError"] = str(exc)[:300]
        # Fail closed: an unscored posting must never be auto-approved.
        log.warning("Scam detector processing failed (%s) — routing all to manual review", exc)
        return [
            {
                **c,
                "scam_score": 50.0,
                "decision": "review",
                "confidence": 0.0,
                "explanation_summary": "Scam detector unavailable; manual review required.",
            }
            for c in candidates
        ]


def _assemble_moderation(doc: dict, source: str) -> dict:
    """
    Stage 3: fold the detector output into doc["moderation"] and drop detector-only keys.
    Returns {status, decision, score, rules, lowConfidence, hardDisqualified} for stats and results.
    """
    scam_score = float(doc.get("scam_score", 0.0))
    decision = doc.get("decision", "review")  # "clear" | "review" | "block"
    confidence = float(doc.get("confidence", 1.0))
    summary_exp = str(doc.get("explanation_summary", ""))

    raw_flags = []
    if isinstance(doc.get("moderation"), dict):
        raw_flags = doc["moderation"].get("flags") or []

    free_mail = bool(doc.get("_freeMail"))
    if free_mail:
        raw_flags = [*raw_flags, "free_email_domain"]

    scam_details: ScamDetailsDict = {
        "score":              scam_score,
        "decision":           decision if decision in ("clear", "review", "block") else "review",
        "confidence":         confidence,
        "explanationSummary": summary_exp,
        "scamFlags":          raw_flags,
        "evaluatedAt":        datetime.now(timezone.utc).isoformat(),
        "riskBreakdown":      {"anomalyScore": (doc.get("risk_breakdown") or {}).get("anomaly_score")},
    }

    mod_status = {"clear": "auto_approved", "block": "auto_rejected"}.get(decision, "pending_review")
    # A free-mail apply address can't be tied to the company: a moderator must look, never auto-approve
    if free_mail and mod_status == "auto_approved":
        mod_status = "pending_review"

    doc["moderation"] = {
        "status":          mod_status,
        "score":           scam_score,
        "flags":           raw_flags,
        "source":          _moderation_source(source),
        "reviewedBy":      None,
        "reviewedAt":      datetime.now(timezone.utc).isoformat(),
        "rejectionReason": summary_exp if decision == "block" else None,
        "scamDetails":     scam_details,
    }

    # Read before the pop below
    rules = [str(r) for r in (doc.get("triggered_rules") or [])]
    meta = {
        "status": mod_status, "decision": decision, "score": scam_score, "rules": rules,
        "lowConfidence": bool(doc.get("low_confidence_forced_review")),
        "hardDisqualified": bool(doc.get("hard_disqualifying_forced")),
    }
    for k in _DETECTOR_EXTRA_KEYS:
        doc.pop(k, None)
    return meta


# ─── Pipeline entry points ────────────────────────────────────────────────────

def push_to_pipeline(
    items: list,
    source: str,
    col: Collection,
    label: str = "",
) -> dict:
    """
    Scraper path. Process raw scraped internships through validation, de-duplication,
    scam detection and normalization into the canonical Internship schema, storing the
    ones that pass in collection 'internships.mod-unvectorised'.

    `source` is the scraper id (github, internshala, ...), stored on every document.

    Returns stats dict: {saved, duplicate, rejected, errors, detail}
    `detail` is the per-batch breakdown stored in `scrape.runs` (see _new_detail).
    """
    _check_source(source)
    if source == "ifind":
        raise ValueError("'ifind' internships live in ifind's own collection and are never staged; use evaluate_internships()")

    stats = {"saved": 0, "duplicate": 0, "rejected": 0, "errors": 0}
    detail = stats["detail"] = _new_detail()
    companies: set = set()
    flag_counts: Counter = Counter()

    # Ensure collection points to 'internships.mod-unvectorised'
    if col.name != STAGING_COLLECTION:
        col = col.database[STAGING_COLLECTION]
    db = col.database

    def is_duplicate(fp: str) -> bool:
        # Approved listings leave staging, so also check the live collection, and employer-posted
        # internships (so a scraped copy of an employer's posting is not listed twice).
        return bool(
            col.find_one({"fingerprint": fp}, {"_id": 1})
            or db[LIVE_COLLECTION].find_one({"fingerprint": fp}, {"_id": 1})
            or db[PLATFORM_COLLECTION].find_one({"fingerprint": fp}, {"_id": 1})
        )

    outcomes: dict = {}
    valid_candidates = _prepare_candidates(items, source, is_duplicate, stats, detail, outcomes)
    if not valid_candidates:
        return stats
    detail["validated"] = len(valid_candidates)

    scored_candidates = _score_candidates(valid_candidates, detail)

    # ── Final Moderation assembly & insertion into MongoDB ──────────────────
    for doc in scored_candidates:
        try:
            meta = _assemble_moderation(doc, source)
            doc.pop("_ix", None)

            col.insert_one(doc)
            _record_saved(detail, doc, meta, flag_counts, companies)
            log.info("  ✅ [%s] scam_score:%.1f — '%s' @ %s", meta["status"], meta["score"], doc["name"], doc["company"])
            stats["saved"] += 1

        except Exception as e:
            log.error("  💥 Error inserting '%s': %s", doc.get("name", "?"), e)
            stats["errors"] += 1

    detail["uniqueCompanies"] = len(companies)
    detail["topTriggeredRules"] = dict(flag_counts.most_common(15))
    return stats


def evaluate_internships(items: list, source: str = "ifind", with_documents: bool = False) -> dict:
    """
    Platform path (e.g. employer-posted internships). Runs the same validation, normalization and
    scam detection as the scraper path, but stores nothing and does no de-duplication: the caller
    owns the document and decides what to do with the verdict.

    Returns {source, count, summary, detectorFailed, results}; `results[i]` matches `items[i]`:
      outcome  "scored" | "rejected" | "error"
      reason   why an item was not scored (missing_required_fields, invalid_url, ...)
      moderationStatus  auto_approved | pending_review | auto_rejected (only when scored)
      score, decision, confidence, flags, rejectionReason, scamDetails, fingerprint
      triggeredRules, lowConfidence, hardDisqualified   how the verdict was reached
      document          (only with_documents=True) the normalized internship exactly as the pipeline would store it
    """
    _check_source(source)
    stats = {"saved": 0, "duplicate": 0, "rejected": 0, "errors": 0}
    detail = _new_detail()
    outcomes: dict = {}

    candidates = _prepare_candidates(items, source, None, stats, detail, outcomes)
    scored = _score_candidates(candidates, detail) if candidates else []

    results: list = [None] * len(items)
    for doc in scored:
        ix = doc.get("_ix")
        if ix is None:
            continue
        try:
            meta = _assemble_moderation(doc, source)
            mod = doc["moderation"]
            results[ix] = {
                "outcome": "scored",
                "reason": None,
                "moderationStatus": meta["status"],
                "score": meta["score"],
                "decision": meta["decision"],
                "confidence": mod["scamDetails"]["confidence"],
                "flags": mod["flags"],
                "rejectionReason": mod["rejectionReason"],
                "scamDetails": mod["scamDetails"],
                "fingerprint": doc.get("fingerprint"),
                "triggeredRules": meta["rules"],
                "lowConfidence": meta["lowConfidence"],
                "hardDisqualified": meta["hardDisqualified"],
            }
            if with_documents:
                # default=str: the detector may leave numpy scalars / datetimes behind
                results[ix]["document"] = json.loads(json.dumps({k: v for k, v in doc.items() if k != "_ix"}, default=str))
        except Exception as e:
            log.error("  💥 Error assembling result for '%s': %s", doc.get("name", "?"), e)
            outcomes[ix] = {"outcome": "error", "reason": f"{type(e).__name__}: {e}"[:200]}

    for ix, item in enumerate(items):
        base = {"index": ix, "id": item.get("id") or item.get("_id") or item.get("platformId")}
        results[ix] = {**base, **(results[ix] or outcomes.get(ix) or {"outcome": "error", "reason": "not processed"})}
        r = results[ix]
        # An employer post is only auto-approved for a verified company. The caller must say so with
        # companyVerified: true; anything else (missing included) is treated as unverified.
        if source == "ifind" and r["outcome"] == "scored" and r["moderationStatus"] == "auto_approved" and item.get("companyVerified") is not True:
            r["moderationStatus"] = "pending_review"
            r["flags"] = [*(r.get("flags") or []), "unverified_company"]
            if r.get("scamDetails"):
                r["scamDetails"] = {**r["scamDetails"], "scamFlags": r["flags"]}

    summary = {"auto_approved": 0, "pending_review": 0, "auto_rejected": 0, "rejected": 0, "errors": 0}
    for r in results:
        if r["outcome"] == "scored":
            summary[r["moderationStatus"]] += 1
        elif r["outcome"] == "rejected":
            summary["rejected"] += 1
        else:
            summary["errors"] += 1

    return {
        "source": source,
        "count": len(items),
        "summary": summary,
        "detectorFailed": detail["detectorFailed"],
        "detectorError": detail["detectorError"],
        "detectorSeconds": detail["detectorSeconds"],
        "results": results,
    }


# ─── Platform path: record the verdict and hand auto-approved posts to the vectoriser ───

_platform_col = None


def _platform_collection():
    """'internships.this-platform' (written by ifind). None when MONGODB_URI is not configured."""
    global _platform_col
    if _platform_col is None:
        uri = os.environ.get("MONGODB_URI")
        if not uri:
            return None
        import pymongo
        client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=10000)
        _platform_col = client[os.environ.get("MONGODB_DB_NAME", "ifind")][PLATFORM_COLLECTION]
    return _platform_col


def _trigger_platform_vectorizer(ids: list) -> bool:
    """POST {VECTORISER_URL}/vectorize-platform: vectorize in place, add to the graph, run every student through it."""
    from services import vectoriser_url
    url = vectoriser_url()
    if not url or not ids:
        return False
    try:
        import requests
        res = requests.post(f"{url}/vectorize-platform", json={"ids": ids, "background": True}, timeout=60)
        res.raise_for_status()
        log.info("🧭 Vectorizer triggered for %d auto-approved employer post(s)", len(ids))
        return True
    except Exception as exc:
        log.warning("Vectorizer trigger failed (%s): the ifind admin refresh will pick the posts up", exc)
        return False


def apply_platform_verdicts(result: dict) -> dict:
    """
    For source "ifind": write each scored verdict onto its document in 'internships.this-platform' (only while it
    is still pending_review, so a moderator's decision is never overwritten), then send the auto-approved ones
    to the vectoriser in the background. Adds `persisted` / `vectorizerTriggered` to each result.
    ifind still saves the same verdict on its own copy; writing it here first means the vectoriser, which only
    processes approved documents, never races ifind's save.
    """
    import threading
    from bson import ObjectId

    col = _platform_collection()
    approved: list = []
    for r in result.get("results", []):
        r["persisted"] = False
        r["vectorizerTriggered"] = False
        rid = r.get("id")
        if col is None or r.get("outcome") != "scored" or not rid or not ObjectId.is_valid(str(rid)):
            continue
        try:
            upd = col.update_one(
                {"_id": ObjectId(str(rid)), "moderation.status": "pending_review"},
                {"$set": {
                    "moderation.status": r["moderationStatus"],
                    "moderation.source": "employer",
                    "moderation.score": r["score"],
                    "moderation.flags": r["flags"],
                    "moderation.rejectionReason": r["rejectionReason"] if r["moderationStatus"] == "auto_rejected" else None,
                    "moderation.scamDetails": r["scamDetails"],
                }},
            )
            r["persisted"] = bool(upd.modified_count)
            if r["persisted"] and r["moderationStatus"] == "auto_approved":
                approved.append(str(rid))
        except Exception as exc:
            log.warning("Could not record the verdict for %s: %s", rid, exc)

    if approved:
        threading.Thread(target=_trigger_platform_vectorizer, args=(approved,), daemon=True).start()
        for r in result["results"]:
            r["vectorizerTriggered"] = str(r.get("id")) in approved
    return result
