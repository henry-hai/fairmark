from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fairmark import clients
from fairmark.cli import main


PACIFIC = ZoneInfo("America/Los_Angeles")


def run_mocked_slate(monkeypatch, tmp_path, capsys, pinnacle_start, bovada_start, kalshi_time, is_live=False):
    monkeypatch.setattr(clients, "SPORT_IDS", (12,))
    monkeypatch.setattr(clients, "BOVADA_PATHS", ("ufc-mma",))
    matchup = {
        "id": 1, "startTime": pinnacle_start.isoformat() if pinnacle_start else None,
        "isLive": is_live, "league": {"sport": {"name": "MMA"}},
        "participants": [{"id": 11, "name": "Fighter A"}, {"id": 12, "name": "Rival"}],
    }
    market = {"matchupId": 1, "type": "moneyline", "prices": [
        {"participantId": 11, "price": -150}, {"participantId": 12, "price": 130},
    ]}
    bovada = {"id": 2, "description": "Fighter A vs Rival", "sport": "MMA",
              "startTime": int(bovada_start.timestamp() * 1000) if bovada_start else None,
              "numMarkets": 1, "displayGroups": [{"markets": [{
                  "id": 3, "status": "O", "description": "Winner", "outcomes": [
                      {"id": 11, "status": "O", "description": "Fighter A", "price": {"american": "-150"}},
                      {"id": 12, "status": "O", "description": "Rival", "price": {"american": "+130"}},
                  ],
              }]}]}
    kalshi = {"category": "Sports", "title": "Fighter A vs Rival", "series_ticker": "KXUFC",
              "markets": [{"ticker": "FIGHTER_A", "title": "Fighter A wins", "yes_sub_title": "Fighter A",
                           "occurrence_datetime": kalshi_time.isoformat(), "yes_ask_dollars": "0.6"}]}
    monkeypatch.setattr(clients, "pinnacle_sport", lambda sport_id: ([matchup], [market]))
    monkeypatch.setattr(clients, "bovada_sport", lambda path: [{"events": [bovada]}])
    monkeypatch.setattr(clients, "kalshi_events", lambda sports: [kalshi])
    slate = tmp_path / "slate.txt"
    slate.write_text("slip 1 50: Fighter A\n")
    assert main(["slate", str(slate), "--trials", "100"]) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("source", ["Pinnacle", "Bovada", "Kalshi"])
def test_start_source_priority_and_buy_by(monkeypatch, tmp_path, capsys, source):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    pinnacle_start = now + timedelta(hours=4) if source == "Pinnacle" else None
    bovada_start = now + timedelta(hours=5) if source != "Kalshi" else None
    kalshi_time = now + timedelta(hours=9)
    output = run_mocked_slate(monkeypatch, tmp_path, capsys, pinnacle_start, bovada_start, kalshi_time)
    chosen = pinnacle_start or bovada_start or kalshi_time - timedelta(hours=3)
    start_label = chosen.astimezone(PACIFIC).strftime("%Y-%m-%d %I:%M %p PT")
    buy_by_label = (chosen - timedelta(minutes=5)).astimezone(PACIFIC).strftime("%Y-%m-%d %I:%M %p PT")
    assert start_label in output
    assert buy_by_label in output
    assert ("estimate from Kalshi" in output) == (source == "Kalshi")


@pytest.mark.parametrize("is_live, start_offset", [(False, -1), (True, 2)])
def test_live_status_overrides_future_kalshi_time(monkeypatch, tmp_path, capsys, is_live, start_offset):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    output = run_mocked_slate(monkeypatch, tmp_path, capsys,
                              now + timedelta(hours=start_offset), now + timedelta(hours=3),
                              now + timedelta(hours=4), is_live=is_live)
    assert "Fighter A vs Rival" in output
    assert "Kalshi event is live" in output
    assert "upcoming" not in output
    assert "Monte Carlo unavailable" in output
