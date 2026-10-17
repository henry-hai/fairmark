"""Rank market outcomes against the leg names in a slate."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from fairmark.markets import Quote


PACIFIC = ZoneInfo("America/Los_Angeles")


def tokens(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return {"draw" if word == "tie" else "knockout" if word in {"ko", "tko"} else word
            for word in words if word not in {"vs", "v", "the", "or", "to", "beat"}}


def candidate_score(query: str, quote: Quote) -> float:
    wanted = tokens(query)
    outcome = tokens(quote.outcome)
    label = tokens(quote.label)
    if not wanted or not wanted & outcome:
        return 0.0
    outcome_coverage = len(wanted & outcome) / len(wanted)
    label_coverage = len(wanted & label) / len(wanted)
    similarity = SequenceMatcher(None, query.casefold(), quote.outcome.casefold()).ratio()
    score = 0.5 * outcome_coverage + 0.25 * label_coverage + 0.2 * similarity
    if quote.kind == "winner" and not wanted & {"knockout", "submission", "decision", "over", "under", "yards", "points"}:
        score += 0.05
    if len(wanted) == 1 and quote.kind == "prop":
        score -= 0.12
    return score


def rank_quotes(query: str, quotes: list[Quote]) -> list[tuple[float, Quote]]:
    ranked = [(candidate_score(query, quote), quote) for quote in quotes]
    return sorted((item for item in ranked if item[0] >= 0.55), key=lambda item: item[0], reverse=True)


def best_quote(query: str, quotes: list[Quote], now: datetime | None = None) -> tuple[Quote | None, list[Quote]]:
    ranked = rank_quotes(query, quotes)
    if len(tokens(query)) <= 2 and not tokens(query) & {"knockout", "submission", "decision", "over", "under", "yards", "points"}:
        ranked = [item for item in ranked if item[1].kind == "winner"]
    if not ranked:
        return None, []
    current = (now or datetime.now(PACIFIC)).astimezone(timezone.utc)
    cutoff = current + timedelta(hours=36)

    def priority(item: tuple[float, Quote]) -> tuple[bool, bool, float]:
        score, quote = item
        start = quote.start.astimezone(timezone.utc) if quote.start else None
        upcoming = start is not None and current <= start <= cutoff
        outcome_words = tokens(quote.outcome)
        exact = outcome_words == tokens(query) or outcome_words - {"team"} == tokens(query)
        return upcoming, exact, quote.volume

    ordered = sorted(ranked, key=priority, reverse=True)
    tied = [quote for item in ordered if priority(item) == priority(ordered[0])
            for quote in [item[1]]]
    if len({(quote.outcome, quote.event) for quote in tied}) > 1:
        return None, tied
    chosen = ordered[0][1]
    alternatives = [quote for _, quote in ordered[1:] if (quote.outcome, quote.event) != (chosen.outcome, chosen.event)]
    return chosen, alternatives
