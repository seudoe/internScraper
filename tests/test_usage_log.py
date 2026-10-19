"""usage_log: cost estimates, per-call recording, and the daily budget that stops system LLM calls."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
mongomock = pytest.importorskip("mongomock")

import usage_log  # noqa: E402


@pytest.fixture()
def db(monkeypatch):
    fake = mongomock.MongoClient()["ifind"]
    monkeypatch.setattr(usage_log, "_db", lambda: fake)
    monkeypatch.setattr(usage_log, "_indexes_ready", False)
    monkeypatch.setattr(usage_log, "_settings_cache", (0.0, usage_log.DEFAULT_DAILY_BUDGET_USD))
    return fake


def cohere_response(tokens_in=1000, tokens_out=500):
    units = SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out)
    return SimpleNamespace(text="{}", meta=SimpleNamespace(billed_units=units, tokens=None))


class FakeCohere:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response or cohere_response(), error, 0

    def chat(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def test_ist_day_rolls_over_at_1830_utc():
    assert usage_log.ist_day(datetime(2026, 10, 19, 18, 29, tzinfo=timezone.utc)) == "2026-10-19"
    assert usage_log.ist_day(datetime(2026, 10, 19, 18, 30, tzinfo=timezone.utc)) == "2026-10-20"


def test_cost_estimate_matches_the_ifind_price_table():
    assert usage_log.estimate_cost_usd("cohere", "command-a-03-2025", 1_000_000, 1_000_000) == 12.5
    assert usage_log.estimate_cost_usd("cohere", "mystery-model", 1000, 1000) > 0


def test_tracked_chat_records_the_call_and_the_daily_totals(db):
    co = FakeCohere(cohere_response(2000, 400))
    usage_log.actor.set("12345")
    resp = usage_log.tracked_chat(co, "telegram", "extract", model="command-a-03-2025", message="hi")

    assert resp.text == "{}"
    call = db["llm.usage"].find_one({"subject": "telegram"})
    assert (call["scope"], call["kind"], call["feature"], call["status"]) == ("system", "call", "extract", "ok")
    assert (call["tokensIn"], call["tokensOut"], call["actor"]) == (2000, 400, "12345")
    assert call["costUsd"] == pytest.approx(0.009)

    today = usage_log.ist_day()
    for subject in ("telegram", "all"):
        day = db["usage.daily"].find_one({"scope": "system", "subject": subject, "day": today})
        assert day["llmCalls"] == 1 and day["tokensIn"] == 2000 and day["costUsd"] == pytest.approx(0.009)


def test_failed_calls_are_logged_and_still_raise(db):
    co = FakeCohere(error=RuntimeError("rate limited"))
    with pytest.raises(RuntimeError):
        usage_log.tracked_chat(co, "github", "enrich", model="command-a-03-2025", message="x")
    call = db["llm.usage"].find_one({"subject": "github"})
    assert call["status"] == "error" and "rate limited" in call["error"]


def test_budget_stops_calls_once_spent_and_flags_the_day(db):
    db["usage.daily"].insert_one({"scope": "system", "subject": "all", "day": usage_log.ist_day(), "costUsd": 3.0})
    co = FakeCohere()
    with pytest.raises(usage_log.BudgetExceeded):
        usage_log.tracked_chat(co, "telegram", "extract", model="command-a-03-2025", message="x")
    assert co.calls == 0
    assert db["usage.daily"].find_one({"subject": "all"})["budgetExceeded"] is True
    assert usage_log.budget_exceeded() is True


def test_budget_is_read_from_the_moderator_setting(db):
    db["usage.settings"].insert_one({"_id": "global", "systemDailyBudgetUsd": 10})
    db["usage.daily"].insert_one({"scope": "system", "subject": "all", "day": usage_log.ist_day(), "costUsd": 5.0})
    assert usage_log.budget_exceeded() is False


def test_yesterdays_spend_does_not_count(db):
    db["usage.daily"].insert_one({"scope": "system", "subject": "all", "day": "2000-01-01", "costUsd": 99.0})
    assert usage_log.budget_exceeded() is False


def test_an_unreachable_database_never_blocks_or_breaks_the_call(monkeypatch):
    def down():
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(usage_log, "_db", down)
    monkeypatch.setattr(usage_log, "_settings_cache", (0.0, usage_log.DEFAULT_DAILY_BUDGET_USD))
    co = FakeCohere()
    assert usage_log.tracked_chat(co, "telegram", "extract", model="command-a-03-2025", message="x").text == "{}"
    assert co.calls == 1
