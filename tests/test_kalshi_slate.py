from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from fairmark import clients
from fairmark.cli import render, resolve
from fairmark.input import Slip
from fairmark.markets import Quote, kalshi_quotes


def test_kalshi_fetch_paginates_once_per_series_and_limits_rate(monkeypatch):
    clock = [100.0]
    calls = []
    sleeps = []

    monkeypatch.setattr(clients, "KALSHI_SERIES", {"esports": ("A", "B")})
    monkeypatch.setattr(clients.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def get(url):
        calls.append((clock[0], parse_qs(urlparse(url).query)))
        query = calls[-1][1]
        if query["series_ticker"] == ["A"] and "cursor" not in query:
            return {"events": [{"title": "first"}], "cursor": "next"}
        return {"events": [{"title": "last"}], "cursor": ""}

    monkeypatch.setattr(clients.time, "sleep", sleep)
    monkeypatch.setattr(clients, "get_json", get)
    assert len(clients.kalshi_events({"esports", "esports"})) == 3
    assert [item[1]["series_ticker"] for item in calls] == [["A"], ["A"], ["B"]]
    assert calls[1][1]["cursor"] == ["next"]
    assert all(item[1]["with_nested_markets"] == ["true"] for item in calls)
    assert all(b[0] - a[0] >= 0.2 for a, b in zip(calls, calls[1:]))
    assert len(sleeps) == 2


def test_kalshi_retries_429_and_honors_retry_after(monkeypatch):
    attempts = []
    sleeps = []
    monkeypatch.setattr(clients.time, "sleep", sleeps.append)
    monkeypatch.setattr(clients.random, "uniform", lambda low, high: 0.1)

    def get(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise HTTPError(url, 429, "Too Many Requests", {"Retry-After": "3"}, None)
        return {"events": []}

    monkeypatch.setattr(clients, "get_json", get)
    limiter = clients.KalshiRateLimiter()
    assert clients._kalshi_get("https://kalshi.test/events", limiter) == {"events": []}
    assert len(attempts) == 2
    assert 3 in sleeps


def test_live_and_settled_legs_are_excluded_from_hit_chance():
    now = datetime.now(timezone.utc)
    events = [{
        "category": "Sports", "title": "Azure vs Ember", "series_ticker": "KXCS2GAME",
        "markets": [{"ticker": "live", "title": "Azure vs Ember", "yes_sub_title": "Azure",
                     "occurrence_datetime": (now - timedelta(hours=1)).isoformat(), "yes_ask_dollars": "0.7"}],
    }, {
        "category": "Sports", "title": "Fighter A vs Rival", "series_ticker": "KXUFC",
        "markets": [{"ticker": "settled", "title": "Fighter A vs Rival", "yes_sub_title": "Fighter A",
                     "occurrence_datetime": (now - timedelta(hours=2)).isoformat(),
                     "status": "settled", "result": "yes"}],
    }]
    kalshi = kalshi_quotes(events)
    fair = [Quote("Pinnacle", name, "moneyline", f"{name} vs Rival", "E Sports" if name == "Azure" else "MMA",
                  now - timedelta(hours=1), "winner", name, probability=0.6) for name in ("Azure", "Fighter A")]
    slips = [Slip("1", 50, ("Azure",)), Slip("2", 50, ("Fighter A",))]
    resolved, _ = resolve(["Azure", "Fighter A"], {"Kalshi": kalshi, "Bovada": [], "Pinnacle": fair}, slips)
    output = render(slips, resolved, 100, 17)
    assert "Azure vs Ember" in output and "Yes: Azure" in output
    assert "Fighter A vs Rival" in output and "Yes: Fighter A" in output
    assert "live" in output and "settled" in output
    assert "Kalshi event is live" in output and "Kalshi event is settled" in output
    assert "Monte Carlo unavailable" in output
    assert "60.0%" not in output


def test_ambiguous_kalshi_candidates_stop_pricing():
    start = datetime.now(timezone.utc) + timedelta(hours=2)
    candidates = [Quote("Kalshi", "Azure", "winner", f"Azure vs {opponent}", "KXCS2GAME",
                        start, "winner", opponent, price=0.6, volume=10) for opponent in ("Ember", "Rival")]
    resolved, notes = resolve(["Azure"], {"Kalshi": candidates, "Bovada": [], "Pinnacle": []})
    output = render([Slip("1", 50, ("Azure",))], resolved, 100, 17)
    assert "Ambiguous Azure on Kalshi" in notes[0]
    assert "Azure vs Ember" in output and "Azure vs Rival" in output
    assert "ambiguous Kalshi match" in output
    assert "Monte Carlo unavailable" in output


def test_buy_by_uses_pinnacle_start():
    start = datetime.now(timezone.utc) + timedelta(hours=6)
    sportsbook_start = start - timedelta(hours=3)
    kalshi = Quote("Kalshi", "Fighter A", "winner", "Fighter A vs Rival", "KXUFC",
                   start, "winner", "kalshi", price=0.7)
    pinnacle = Quote("Pinnacle", "Fighter A", "moneyline", "Fighter A vs Rival", "MMA",
                     sportsbook_start, "winner", "pinnacle", probability=0.7)
    output = render([Slip("1", 50, ("Fighter A",))],
                    {"Fighter A": {"Kalshi": kalshi, "Bovada": None, "Pinnacle": pinnacle}}, 100, 17)
    expected = (sportsbook_start.astimezone(ZoneInfo("America/Los_Angeles"))
                - timedelta(minutes=5)).strftime("%Y-%m-%d %I:%M %p PT")
    assert expected in output
    assert sportsbook_start.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %I:%M %p PT") in output
    assert "estimate from Kalshi" not in output
