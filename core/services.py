"""
Base URLs of the other services, read from the environment on every call (nothing hardcoded).

  MAIN_URL         the main project (ifind): browser origin(s) allowed by CORS, comma-separated
  VECTORISER_URL   the vectoriser (vectors, HNSW graph, student recommendations)

An unset variable means "not configured": callers skip that integration instead of calling a stale default.
The old names MAIN_SERVER / VECTORIZER_URL are still read when the new ones are missing.
"""

import os
from typing import List, Optional


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().rstrip("/")
    return value or None


def vectoriser_url() -> Optional[str]:
    return _clean(os.environ.get("VECTORISER_URL") or os.environ.get("VECTORIZER_URL"))


def main_urls() -> List[str]:
    raw = os.environ.get("MAIN_URL") or os.environ.get("MAIN_SERVER") or ""
    return [u for u in (_clean(p) for p in raw.split(",")) if u]
