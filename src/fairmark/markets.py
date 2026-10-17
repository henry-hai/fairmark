"""Turn public market responses into comparable outcome quotes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from fairmark.pricing import implied_probability, remove_vig


@dataclass(frozen=True)
class Quote:
    source: str
    outcome: str
    market: str
    event: str
    sport: str
    start: datetime | None
    kind: str
    identifier: str
    price: float | None = None
    odds: str | None = None
    probability: float | None = None
    volume: float = 0.0
    fair_probability: float | None = None
    status: str | None = None
    is_live: bool = False

    @property
    def label(self) -> str:
        return " ".join((self.outcome, self.market, self.event, self.sport))


def parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, int):
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None


def kalshi_quotes(events: list[dict]) -> list[Quote]:
    quotes = []
    for event in events:
        if event.get("category", "").lower() not in {"sports", "esports"}:
            continue
        for market in event.get("markets", []):
            ask = market.get("yes_ask_dollars")
            title = market.get("title") or event.get("title", "")
            outcome = market.get("yes_sub_title") or market.get("subtitle") or title
            kind = "prop" if re.search(r"\b(by|yards|points|goals|round|ko|tko|submission|decision|over|under)\b", title, re.I) else "winner"
            quotes.append(Quote(
                source="Kalshi",
                outcome=outcome,
                market=title,
                event=event.get("title", ""),
                sport=event.get("series_ticker", ""),
                start=parse_time(market.get("occurrence_datetime") or event.get("strike_date")),
                kind=kind,
                identifier=market.get("ticker", ""),
                price=float(ask) if ask and 0 < float(ask) < 1 else None,
                volume=float(market.get("volume_fp") or market.get("volume") or 0),
                status="settled" if market.get("result") or market.get("status") in {"settled", "finalized"}
                       else market.get("status"),
            ))
    return quotes


def pinnacle_quotes(matchups: list[dict], markets: list[dict]) -> list[Quote]:
    by_id = {matchup["id"]: matchup for matchup in matchups}
    quotes = []
    for market in markets:
        matchup = by_id.get(market.get("matchupId"))
        prices = market.get("prices", [])
        if not matchup or len(prices) < 2:
            continue
        participants = {participant.get("id"): participant.get("name", "") for participant in matchup.get("participants", []) if participant.get("id") is not None}
        alignments = {participant.get("alignment"): participant.get("name", "") for participant in matchup.get("participants", [])}
        names = []
        for price in prices:
            name = participants.get(price.get("participantId"))
            if not name:
                designation = price.get("designation", "")
                name = alignments.get(designation) or designation.title()
            if not name or not price.get("price"):
                break
            if price.get("points") is not None:
                name = f"{name} {price['points']}"
            names.append(name)
        if len(names) != len(prices):
            continue
        fair = remove_vig([price["price"] for price in prices])
        special = matchup.get("special") or {}
        parent = matchup.get("parent") or {}
        parent_names = [participant.get("name", "") for participant in parent.get("participants", [])]
        event = " vs ".join(parent_names) if parent_names else " vs ".join(
            participant.get("name", "") for participant in matchup.get("participants", []) if participant.get("name") != "Draw"
        )
        description = special.get("description", "")
        is_prop = bool(description) or market.get("type") != "moneyline" or market.get("period", 0) != 0 or bool(matchup.get("parentId"))
        for price, name, probability in zip(prices, names, fair, strict=True):
            outcome = description if description and name.lower() == "yes" else name
            if description and name.lower() != "yes":
                outcome = f"{name} {description}"
            if not outcome:
                continue
            quotes.append(Quote(
                source="Pinnacle",
                outcome=outcome,
                market=description or market.get("type", ""),
                event=event,
                sport=(matchup.get("league") or {}).get("sport", {}).get("name", ""),
                start=parse_time(matchup.get("startTime") or parent.get("startTime")),
                kind="prop" if is_prop else "winner",
                identifier=f"{matchup['id']}:{price.get('participantId') or price.get('designation')}",
                odds=str(price["price"]),
                probability=probability,
                is_live=bool(matchup.get("isLive") or parent.get("isLive")),
            ))
    return quotes


def bovada_events(pages: list[dict]) -> list[dict]:
    seen = {}
    for page in pages:
        for event in page.get("events", []):
            seen[event.get("id")] = event
    return list(seen.values())


def bovada_quotes(events: list[dict]) -> list[Quote]:
    quotes = []
    for event in events:
        for group in event.get("displayGroups", []):
            for market in group.get("markets", []):
                if market.get("status") != "O":
                    continue
                description = market.get("description", "")
                if market.get("period", {}).get("main") is False:
                    continue
                priced = [
                    (outcome, (outcome.get("price") or {}).get("american"))
                    for outcome in market.get("outcomes", [])
                    if outcome.get("status") == "O" and (outcome.get("price") or {}).get("american")
                ]
                fair = remove_vig([odds for _, odds in priced]) if len(priced) >= 2 else [None] * len(priced)
                for (outcome, odds), fair_probability in zip(priced, fair, strict=True):
                    price = outcome["price"]
                    handicap = price.get("handicap")
                    name = outcome.get("description", "")
                    if handicap is not None:
                        name = f"{name} {handicap}"
                    is_prop = re.fullmatch(r"(?:fight |game )?(?:winner|moneyline)|to win the bout|match result", description, re.I) is None
                    quotes.append(Quote(
                        source="Bovada",
                        outcome=name,
                        market=description,
                        event=event.get("description", ""),
                        sport=event.get("sport", ""),
                        start=parse_time(event.get("startTime")),
                        kind="prop" if is_prop else "winner",
                        identifier=f"{event.get('id')}:{market.get('id')}:{outcome.get('id')}",
                        odds=str(odds),
                        probability=implied_probability(odds),
                        fair_probability=fair_probability,
                    ))
    return quotes
