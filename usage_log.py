"""
usage_log.py — usage log and daily budget for the system's own LLM calls (the Telegram bot and the scrapers' enrichment).

Writes the same MongoDB collections the ifind app reads for its Usage page:
  llm.usage       one document per provider call (90-day TTL), scope "system", subject = the source (telegram, github, ...)
  usage.daily     per-day totals: one document per source plus one named "all" that the daily budget is checked against
  usage.settings  document "global"; `systemDailyBudgetUsd` is the daily budget (default 3 USD), editable by moderators

Wrap a Cohere call with `tracked_chat(co, source, feature, **chat_kwargs)`. When today's estimated spend has reached
the budget it raises BudgetExceeded instead of calling the provider; the budget resets at midnight IST.
Logging and the budget lookup never break a caller: if MongoDB can't be reached the call goes ahead unrecorded.
"""

import contextvars
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("usage_log")

IST = timezone(timedelta(hours=5, minutes=30))
DB_NAME = "ifind"
DEFAULT_DAILY_BUDGET_USD = 3.0
SETTINGS_TTL_SECONDS = 30
LOG_TTL_SECONDS = 90 * 24 * 60 * 60

# USD per million tokens (input, output). Estimates for budgeting, not invoices; keep in step with ifind/lib/usage/pricing.ts.
PRICES = {("cohere", "command-a-03-2025"): (2.5, 10.0)}
FALLBACK_PRICE = (2.5, 10.0)      # an unknown model is never logged as free

# Who triggered the call (a Telegram user id, say); set by the entry point, stored on each call document.
actor: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("usage_actor", default=None)


class BudgetExceeded(RuntimeError):
    """Today's estimated spend on system LLM calls has reached the daily budget."""


def ist_day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(IST).strftime("%Y-%m-%d")


def estimate_cost_usd(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get((provider, model), FALLBACK_PRICE)
    return round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 8)


# ─── MongoDB ──────────────────────────────────────────────────────────────────

_client = None
_client_lock = threading.Lock()
_indexes_ready = False
_settings_cache: tuple = (0.0, DEFAULT_DAILY_BUDGET_USD)


def _db():
    """Shared client for the usage collections; raises when MONGODB_URI is missing or unreachable."""
    global _client
    with _client_lock:
        if _client is None:
            import pymongo

            uri = os.environ.get("MONGODB_URI")
            if not uri:
                from dotenv import load_dotenv

                load_dotenv()
                uri = os.environ.get("MONGODB_URI")
            if not uri:
                raise RuntimeError("MONGODB_URI not set")
            _client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
        return _client[DB_NAME]


def _ensure_indexes(db) -> None:
    """The same indexes the ifind models declare (matching specs, so neither side errors on the other's)."""
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        db["llm.usage"].create_index([("ts", 1)], expireAfterSeconds=LOG_TTL_SECONDS)
        db["llm.usage"].create_index([("scope", 1), ("subject", 1), ("ts", -1)])
        db["usage.daily"].create_index([("scope", 1), ("subject", 1), ("day", 1)], unique=True)
        db["usage.daily"].create_index([("day", 1), ("costUsd", -1)])
        _indexes_ready = True
    except Exception as exc:
        log.warning("could not create usage indexes (%s)", exc)


def daily_budget_usd() -> float:
    """The moderator-set budget, cached for 30 seconds; the default if none is saved or Mongo is unreachable."""
    global _settings_cache
    fetched_at, value = _settings_cache
    if time.monotonic() - fetched_at < SETTINGS_TTL_SECONDS:
        return value
    try:
        doc = _db()["usage.settings"].find_one({"_id": "global"}, {"systemDailyBudgetUsd": 1}) or {}
        value = float(doc.get("systemDailyBudgetUsd", DEFAULT_DAILY_BUDGET_USD))
    except Exception as exc:
        log.warning("could not read the usage budget (%s); using %s", exc, value)
    _settings_cache = (time.monotonic(), value)
    return value


def spent_today() -> float:
    """Estimated USD spent on system LLM calls so far today (IST)."""
    doc = _db()["usage.daily"].find_one({"scope": "system", "subject": "all", "day": ist_day()}, {"costUsd": 1})
    return float((doc or {}).get("costUsd", 0.0))


def budget_exceeded() -> bool:
    try:
        return spent_today() >= daily_budget_usd()
    except Exception as exc:
        log.warning("budget check failed (%s); allowing the call", exc)
        return False


def ensure_budget(source: str) -> None:
    """Raise BudgetExceeded when today's budget is used up; also flags the day so moderators see it."""
    if not budget_exceeded():
        return
    try:
        _db()["usage.daily"].update_one(
            {"scope": "system", "subject": "all", "day": ist_day()},
            {"$set": {"budgetExceeded": True, "updatedAt": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception:
        pass
    log.warning("daily AI budget reached; %s call refused", source)
    raise BudgetExceeded(f"daily AI budget of ${daily_budget_usd():.2f} reached; resets at midnight IST")


# ─── Recording ────────────────────────────────────────────────────────────────

def _bump(db, subject: str, inc: dict) -> None:
    key = {"scope": "system", "subject": subject, "day": ist_day()}
    update = {"$inc": inc, "$set": {"updatedAt": datetime.now(timezone.utc)}}
    try:
        db["usage.daily"].update_one(key, update, upsert=True)
    except Exception as exc:
        from pymongo.errors import DuplicateKeyError

        if not isinstance(exc, DuplicateKeyError):
            raise
        db["usage.daily"].update_one(key, update, upsert=True)      # two writers raced to create today's document


def record_call(source: str, feature: str, provider: str, model: str, tokens_in: int, tokens_out: int,
                latency_ms: int, ok: bool, error: Optional[str] = None) -> float:
    """Store one provider call and add it to today's totals. Returns the estimated cost; never raises."""
    cost = estimate_cost_usd(provider, model, tokens_in, tokens_out)
    try:
        db = _db()
        _ensure_indexes(db)
        db["llm.usage"].insert_one({
            "ts": datetime.now(timezone.utc),
            "kind": "call",
            "scope": "system",
            "subject": source,
            "feature": feature,
            "provider": provider,
            "model": model,
            "tokensIn": tokens_in,
            "tokensOut": tokens_out,
            "costUsd": cost,
            "latencyMs": latency_ms,
            "status": "ok" if ok else "error",
            "error": (error or None) and str(error)[:200],
            "actor": actor.get(),
        })
        inc = {"llmCalls": 1, "tokensIn": tokens_in, "tokensOut": tokens_out, "costUsd": cost}
        _bump(db, source, inc)
        _bump(db, "all", inc)
    except Exception as exc:
        log.warning("could not record LLM usage (%s)", exc)
    return cost


def _billed_tokens(response) -> tuple:
    """(input, output) tokens from a Cohere chat response."""
    meta = getattr(response, "meta", None)
    for attr in ("billed_units", "tokens"):
        units = getattr(meta, attr, None)
        tin, tout = getattr(units, "input_tokens", None), getattr(units, "output_tokens", None)
        if tin is not None or tout is not None:
            return int(tin or 0), int(tout or 0)
    return 0, 0


def tracked_chat(co, source: str, feature: str, **chat_kwargs):
    """`co.chat(**chat_kwargs)` with the budget enforced first and the tokens, cost and latency recorded."""
    ensure_budget(source)
    model = chat_kwargs.get("model", "")
    started = time.monotonic()
    try:
        response = co.chat(**chat_kwargs)
    except Exception as exc:
        record_call(source, feature, "cohere", model, 0, 0, int((time.monotonic() - started) * 1000), False, repr(exc))
        raise
    tokens_in, tokens_out = _billed_tokens(response)
    record_call(source, feature, "cohere", model, tokens_in, tokens_out, int((time.monotonic() - started) * 1000), True)
    return response
