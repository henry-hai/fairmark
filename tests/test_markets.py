import pytest

from fairmark.markets import Quote, bovada_quotes, kalshi_quotes, pinnacle_quotes
from fairmark.matching import best_quote


def test_pinnacle_designations_and_three_way_soccer():
    matchups = [{
        "id": 42,
        "participants": [
            {"alignment": "home", "name": "Barcelona"},
            {"alignment": "away", "name": "Madrid"},
        ],
        "league": {"sport": {"name": "Soccer"}},
        "startTime": "2026-10-17T20:00:00Z",
        "type": "matchup",
    }]
    markets = [{
        "matchupId": 42,
        "type": "moneyline",
        "prices": [
            {"designation": "home", "price": -110},
            {"designation": "draw", "price": +250},
            {"designation": "away", "price": +300},
        ],
    }]
    quotes = pinnacle_quotes(matchups, markets)
    assert [quote.outcome for quote in quotes] == ["Barcelona", "Draw", "Madrid"]
    assert sum(quote.probability for quote in quotes) == pytest.approx(1)


def test_plain_fighter_name_does_not_choose_a_method_prop():
    prop = Quote("Kalshi", "Fighter B by Decision", "Fighter B wins by decision", "Fighter B vs Fighter A", "Boxing", None, "prop", "prop", price=0.24)
    winner = Quote("Kalshi", "Fighter B", "Will Fighter B beat Fighter A?", "Fighter B vs Fighter A", "Boxing", None, "winner", "winner", price=0.74)
    chosen, rivals = best_quote("Fighter B", [prop, winner])
    assert chosen == winner
    assert rivals == []


def test_pinnacle_period_moneyline_is_not_the_match_winner():
    matchups = [{"id": 1, "participants": [{"alignment": "home", "name": "Azure"},
                                           {"alignment": "away", "name": "BB Team"}]}]
    markets = [{"matchupId": 1, "type": "moneyline", "period": 2,
                "prices": [{"designation": "home", "price": -150}, {"designation": "away", "price": 120}]}]
    quotes = pinnacle_quotes(matchups, markets)
    assert all(quote.kind == "prop" for quote in quotes)
    assert best_quote("Azure", quotes) == (None, [])


def test_bovada_conditional_winner_is_a_prop():
    event = {"id": 1, "description": "Fighter D vs Fighter C", "displayGroups": [{"markets": [{
        "id": 2, "status": "O", "description": "Fight Winner - Decision Only", "outcomes": [
            {"id": 3, "status": "O", "description": "Fighter C", "price": {"american": "-205"}},
            {"id": 4, "status": "O", "description": "Fighter D", "price": {"american": "+175"}},
        ],
    }]}]}
    quotes = bovada_quotes([event])
    assert all(quote.kind == "prop" for quote in quotes)
    assert best_quote("Fighter C", quotes) == (None, [])


def test_kalshi_volume_uses_fractional_field():
    quotes = kalshi_quotes([{"category": "Esports", "title": "Azure vs Rival", "markets": [{
        "ticker": "AZURE", "title": "Azure wins", "yes_sub_title": "Azure",
        "yes_ask_dollars": "0.65", "volume_fp": "120.38",
    }]}])
    assert quotes[0].volume == 120.38
