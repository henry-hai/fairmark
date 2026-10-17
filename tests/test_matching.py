from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fairmark.cli import resolve
from fairmark.input import Slip
from fairmark.markets import Quote
from fairmark.matching import best_quote


NOW = datetime(2026, 10, 17, 9, tzinfo=ZoneInfo("America/Los_Angeles"))


def quote(name, hours, volume, identifier):
    return Quote("Kalshi", name, "Winner", f"{name} vs Rival", "ESPO", NOW + timedelta(hours=hours),
                 "winner", identifier, price=0.5, volume=volume)


def test_upcoming_event_beats_exact_name_outside_window():
    near = quote("Atlanta Azure", 12, 10, "near")
    exact_later = quote("Team Azure", 40, 1000, "later")
    chosen, alternatives = best_quote("Azure", [exact_later, near], NOW)
    assert chosen == near
    assert alternatives == [exact_later]


def test_exact_team_name_beats_partial_names_in_window():
    chosen, alternatives = best_quote("Azure", [
        quote("Azure Force", 3, 1000, "force"),
        quote("Atlanta Azure", 4, 900, "atlanta"),
        quote("Team Azure", 5, 1, "team"),
    ], NOW)
    assert chosen.outcome == "Team Azure"
    assert {alternative.outcome for alternative in alternatives} == {"Azure Force", "Atlanta Azure"}


def test_volume_breaks_remaining_tie_and_resolution_reports_choice(monkeypatch):
    import fairmark.matching as matching

    monkeypatch.setattr(matching, "datetime", type("FixedDatetime", (), {"now": staticmethod(lambda zone: NOW)}))
    low = quote("Atlanta Azure", 3, 10, "low")
    high = quote("Azure Force", 4, 20, "high")
    chosen, alternatives = best_quote("Azure", [low, high], NOW)
    assert chosen == high
    assert alternatives == [low]
    resolved, notes = resolve(["Azure"], {"Kalshi": [low, high], "Bovada": [], "Pinnacle": []})
    assert resolved["Azure"]["Kalshi"] == high
    assert notes == ["Match note: Azure on Kalshi chose Azure Force [Azure Force vs Rival], alternatives: Atlanta Azure [Atlanta Azure vs Rival]"]


def test_slate_context_keeps_ambiguous_name_in_the_same_sport():
    basketball = Quote("Pinnacle", "Adamson Azure", "moneyline", "Adamson vs DLSU", "Basketball",
                       NOW + timedelta(hours=4), "winner", "basketball", probability=0.4)
    esports = Quote("Pinnacle", "Azure", "moneyline", "Azure vs BB Team", "E Sports",
                   NOW - timedelta(hours=1), "winner", "esports", probability=0.7)
    ember = Quote("Bovada", "Ember", "Winner", "Ember vs Rival", "ESPT",
                 NOW + timedelta(hours=8), "winner", "ember", odds="-150", fair_probability=0.57)
    resolved, _ = resolve(["Azure", "Ember"],
                          {"Kalshi": [], "Bovada": [ember], "Pinnacle": [basketball, esports]},
                          [Slip("1", 50, ("Azure", "Ember"))])
    assert resolved["Azure"]["Pinnacle"] == esports
