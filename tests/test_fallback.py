from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from fairmark import clients
from fairmark.cli import main, render
from fairmark.input import Slip
from fairmark.markets import bovada_quotes


def test_pinnacle_retries_headers_and_caches_success(monkeypatch):
    clients._pinnacle_cache.clear()
    calls = []
    delays = []

    def fake_get(url, headers=None):
        calls.append((url, headers))
        if len(calls) < 3:
            raise HTTPError(url, 403, "Forbidden", {}, None)
        return [{"id": 1}]

    monkeypatch.setattr(clients, "get_json", fake_get)
    monkeypatch.setattr(clients.time, "sleep", delays.append)
    url = f"{clients.PINNACLE}/sports/12/matchups"
    assert clients.pinnacle_get(url) == [{"id": 1}]
    assert clients.pinnacle_get(url) == [{"id": 1}]
    assert len(calls) == 3
    assert delays == [1, 2]
    assert all(headers["X-API-Key"] == clients.PINNACLE_GUEST_KEY for _, headers in calls)
    assert all(headers["Referer"] == "https://www.pinnacle.com/" for _, headers in calls)
    assert all("Mozilla/5.0" in headers["User-Agent"] and "application/json" in headers["Accept"] for _, headers in calls)


def test_bovada_no_vig_uses_all_market_outcomes():
    event = {"id": 1, "description": "Azure vs Ember", "displayGroups": [{"markets": [{
        "id": 2, "status": "O", "description": "Match Result", "outcomes": [
            {"id": 3, "status": "O", "description": "Azure", "price": {"american": "-110"}},
            {"id": 4, "status": "O", "description": "Draw", "price": {"american": "+250"}},
            {"id": 5, "status": "O", "description": "Ember", "price": {"american": "+300"}},
        ],
    }]}]}
    quotes = bovada_quotes([event])
    assert sum(quote.fair_probability for quote in quotes) == pytest.approx(1)
    assert quotes[0].fair_probability != pytest.approx(quotes[0].probability)


def test_slate_uses_bovada_fair_after_pinnacle_403(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(clients, "SPORT_IDS", (12,))
    monkeypatch.setattr(clients, "BOVADA_PATHS", ("esports",))
    monkeypatch.setattr(clients, "KALSHI_SERIES", {"esports": ("KXCS2GAME",)})
    monkeypatch.setattr(clients.time, "sleep", lambda seconds: None)
    start = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    event = {"id": "event", "description": "Azure vs Ember", "sport": "Esports", "startTime": start,
             "numMarkets": 1, "displayGroups": [{"markets": [{"id": "winner", "status": "O",
             "description": "Winner", "outcomes": [
                 {"id": "azure", "status": "O", "description": "Azure", "price": {"american": "-150"}},
                 {"id": "ember", "status": "O", "description": "Ember", "price": {"american": "+130"}},
             ]}]}]}
    calls = []

    def fake_get(url, headers=None):
        calls.append(url)
        if url.startswith(clients.PINNACLE):
            raise HTTPError(url, 403, "Forbidden", {}, None)
        if url.startswith(clients.BOVADA):
            return [{"events": [event]}]
        return {"events": [{"category": "Esports", "title": "Azure vs Ember", "series_ticker": "KXCS2GAME",
                "markets": [{"ticker": "azure", "title": "Azure vs Ember", "yes_sub_title": "Azure",
                             "yes_ask_dollars": "0.61", "occurrence_datetime": start}]}], "cursor": ""}

    monkeypatch.setattr(clients, "get_json", fake_get)
    slate = tmp_path / "slate.txt"
    slate.write_text("slip 1 50: Azure\n")
    assert main(["slate", str(slate), "--trials", "100"]) == 0
    out = capsys.readouterr()
    assert "(58.0%) Bovada fair" in out.out
    assert "N/A Bovada fair" not in out.out
    assert "Monte Carlo | 100 trials" in out.out
    assert "N/A" not in out.out.split("Estimated slips")[0]
    assert len([url for url in calls if url.startswith(clients.PINNACLE)]) == 3
    assert "Pinnacle sport 12: HTTP Error 403" in out.err


def test_missing_fair_does_not_claim_bovada_fallback():
    output = render([Slip("1", 50, ("Azure",))],
                    {"Azure": {"Kalshi": None, "Bovada": None, "Pinnacle": None}}, 100, 17)
    assert "Bovada fair" not in output
    assert "No fair line for: Azure" in output
