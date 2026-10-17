"""Command line entry point for pricing a slate."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fairmark import clients
from fairmark.input import Slip, read_slips
from fairmark.markets import Quote, bovada_events, bovada_quotes, kalshi_quotes, pinnacle_quotes
from fairmark.matching import best_quote, rank_quotes, tokens
from fairmark.pricing import SimulatedSlip, combo_payout, simulate, value_rating


PACIFIC = ZoneInfo("America/Los_Angeles")


def percent(value: float | None) -> str:
    return f"({value * 100:.1f}%)" if value is not None else "N/A"


def money(value: float) -> str:
    return f"${value:,.2f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    output = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    output.append("  ".join("-" * width for width in widths))
    output.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    return "\n".join(output)


def unique_legs(slips: list[Slip]) -> list[str]:
    names = {}
    for slip in slips:
        for leg in slip.legs:
            names.setdefault(leg.casefold(), leg)
    return list(names.values())


def fetch_quotes(legs: list[str]) -> tuple[dict[str, list[Quote]], list[str]]:
    clients._pinnacle_cache.clear()
    warnings = []
    by_source: dict[str, list[Quote]] = {}
    pinnacle = []
    for sport_id in clients.SPORT_IDS:
        try:
            pinnacle.extend(pinnacle_quotes(*clients.pinnacle_sport(sport_id)))
        except Exception as exc:
            warnings.append(f"Pinnacle sport {sport_id}: {exc}")
    by_source["Pinnacle"] = pinnacle
    raw_bovada = []
    for path in clients.BOVADA_PATHS:
        try:
            raw_bovada.extend(bovada_events(clients.bovada_sport(path)))
        except Exception as exc:
            warnings.append(f"Bovada {path}: {exc}")
    events = {event.get("id"): event for event in raw_bovada}
    for event in list(events.values()):
        names = tokens(event.get("description", ""))
        names.update(token for competitor in event.get("competitors", []) for token in tokens(competitor.get("name", "")))
        if not any(tokens(leg) & names for leg in legs):
            continue
        link = event.get("link")
        if not link or event.get("numMarkets", 0) <= sum(
            len(group.get("markets", [])) for group in event.get("displayGroups", [])
        ):
            continue
        try:
            for full_event in bovada_events(clients.bovada_coupon(link)):
                events[full_event.get("id")] = full_event
        except Exception as exc:
            warnings.append(f"Bovada coupon {event.get('description')}: {exc}")
    by_source["Bovada"] = bovada_quotes(list(events.values()))
    sport_names = {
        "BOXI": "boxing", "Boxing": "boxing",
        "MMA": "mma", "Mixed Martial Arts": "mma",
        "ESPO": "esports", "ESPT": "esports", "Esports": "esports", "E Sports": "esports",
        "FOOT": "football", "Football": "football",
        "SOCC": "soccer", "Soccer": "soccer",
        "BASK": "basketball", "Basketball": "basketball",
    }
    sports = set()
    for leg in legs:
        for source in ("Pinnacle", "Bovada"):
            ranked = rank_quotes(leg, by_source[source])
            if ranked:
                leading = ranked[0][0]
                sports.update(sport_names[quote.sport] for score, quote in ranked[:10]
                              if score >= leading - 0.1 and quote.sport in sport_names)
    if not sports:
        sports = set(clients.KALSHI_SERIES)
    try:
        by_source["Kalshi"] = kalshi_quotes(clients.kalshi_events(sports))
    except Exception as exc:
        by_source["Kalshi"] = []
        warnings.append(f"Kalshi: {exc}")
    return by_source, warnings


def sport_family(sport: str) -> str:
    name = re.sub(r"[^a-z0-9]", "", sport.casefold())
    for family, names in {
        "esports": ("espo", "espt", "esports", "kxcs2", "kxvalorant", "kxlol"),
        "mma": ("mma", "mixedmartialarts", "kxufc"),
        "boxing": ("boxi", "boxing", "kxboxing"),
        "football": ("foot", "football", "kxnfl", "kxncaaf"),
        "soccer": ("socc", "soccer", "kxepl", "kxlaliga"),
        "basketball": ("bask", "basketball", "kxnba", "kxwnba", "kxncaab"),
    }.items():
        if any(name.startswith(prefix) for prefix in names):
            return family
    return name


def resolve(legs: list[str], by_source: dict[str, list[Quote]], slips: list[Slip] | None = None) -> tuple[dict[str, dict[str, Quote | None]], list[str]]:
    resolved = {}
    notes = []
    hints = {}
    for leg in legs:
        kalshi, _ = best_quote(leg, by_source["Kalshi"])
        if kalshi:
            hints[leg] = sport_family(kalshi.sport)
            continue
        families = {sport_family(quote.sport) for source in ("Bovada", "Pinnacle")
                    for _, quote in rank_quotes(leg, by_source[source]) if quote.kind == "winner"}
        if len(families) == 1:
            hints[leg] = families.pop()
    for slip in slips or []:
        families = {hints[leg] for leg in slip.legs if leg in hints}
        if len(families) == 1:
            for leg in slip.legs:
                hints.setdefault(leg, next(iter(families)))
    for leg in legs:
        row = {}
        for source in ("Kalshi", "Bovada", "Pinnacle"):
            candidates = [quote for quote in by_source[source] if sport_family(quote.sport) == hints[leg]] if leg in hints else by_source[source]
            quote, rivals = best_quote(leg, candidates)
            row[source] = quote
            if rivals:
                alternatives = ", ".join(dict.fromkeys(f"{rival.outcome} [{rival.event}]" for rival in rivals))
                if quote:
                    notes.append(f"Match note: {leg} on {source} chose {quote.outcome} [{quote.event}], alternatives: {alternatives}")
                else:
                    notes.append(f"Ambiguous {leg} on {source}. Candidates: {alternatives}. Use a fuller leg name.")
                    if source == "Kalshi":
                        row["ambiguous"] = rivals
        resolved[leg] = row
    return resolved, notes


def actual_start(pinnacle: Quote | None, bovada: Quote | None, kalshi: Quote | None) -> tuple[datetime | None, bool]:
    if pinnacle and pinnacle.start:
        return pinnacle.start, False
    if bovada and bovada.start:
        return bovada.start, False
    if kalshi and kalshi.start:
        return kalshi.start - timedelta(hours=3), True
    return None, False


def render(slips: list[Slip], resolved: dict[str, dict[str, Quote | None]], trials: int, seed: int) -> str:
    now = datetime.now(PACIFIC)
    lines = [f"Fairmark slate | {now:%Y-%m-%d %I:%M %p} Pacific", ""]
    rows = []
    fair_probabilities = {}
    canonical = {}
    simulation_probabilities = {}
    excluded = {}
    starts = {}
    verification_rows = []
    for leg, quotes in resolved.items():
        kalshi, bovada, pinnacle = (quotes[source] for source in ("Kalshi", "Bovada", "Pinnacle"))
        start, estimated = actual_start(pinnacle, bovada, kalshi)
        starts[leg] = (start, estimated)
        if quotes.get("ambiguous"):
            status = "ambiguous"
            excluded[leg] = "ambiguous Kalshi match"
            candidates = ", ".join(f"{quote.event} | Yes: {quote.outcome}" for quote in quotes["ambiguous"])
            verification_rows.append([leg, f"Candidates: {candidates}", "", "", status])
        elif kalshi:
            status = "settled" if kalshi.status in {"settled", "finalized"} else (
                "live" if (pinnacle and pinnacle.is_live) or (start and start.astimezone(PACIFIC) <= now) else
                "upcoming" if start else "unverified")
            if status != "upcoming":
                excluded[leg] = f"Kalshi event is {status}"
            start_label = start.astimezone(PACIFIC).strftime("%Y-%m-%d %I:%M %p PT") if start else "unknown"
            if estimated:
                start_label += " (estimate from Kalshi)"
            verification_rows.append([leg, kalshi.event, f"Yes: {kalshi.outcome}", start_label, status])
        else:
            excluded[leg] = "no matched Kalshi event"
            verification_rows.append([leg, "no match", "", "unknown", "unverified"])
        fair_quote = pinnacle if pinnacle and pinnacle.probability is not None else bovada if bovada and bovada.fair_probability is not None else None
        fair = pinnacle.probability if fair_quote is pinnacle and pinnacle else bovada.fair_probability if bovada else None
        if fair_quote and fair is not None and leg not in excluded:
            fair_probabilities[leg] = fair
            canonical[leg] = f"{fair_quote.source}:{fair_quote.identifier}"
            simulation_probabilities[canonical[leg]] = fair
        rows.append([
            leg,
            f"{kalshi.price * 100:g}c" if kalshi and kalshi.price is not None and leg not in excluded else "N/A",
            bovada.odds if bovada and bovada.odds and leg not in excluded else "N/A",
            percent(bovada.probability if bovada and leg not in excluded else None),
            percent(fair if leg not in excluded else None) + (" Bovada fair" if leg not in excluded and fair is not None and fair_quote is bovada else ""),
        ])
    lines.append(table(["Leg", "Kalshi price (ask)", "Bovada odds", "Bovada implied %", "Pinnacle fair %"], rows))
    lines.extend(["", "Kalshi verification", table(["Leg", "Exact event title", "Market side", "Start Pacific", "Status"], verification_rows)])
    if excluded:
        lines.extend(["", "Excluded from pre-match pricing: " + ". ".join(f"{leg}: {reason}" for leg, reason in excluded.items())])
    lines.extend(["", "Estimated slips"])
    slip_rows = []
    simulated = []
    total_stake = 0.0
    total_profit = 0.0
    missing_fair = set()
    for slip in slips:
        total_stake += slip.stake
        missing = [leg for leg in slip.legs if leg not in fair_probabilities]
        if missing:
            missing_fair.update(missing)
            names = ", ".join(f"{leg} {percent(fair_probabilities.get(leg))}" for leg in slip.legs)
            slip_rows.append([slip.name, names, money(slip.stake), "N/A", "N/A", "N/A", "unknown", "N/A"])
            continue
        if len({canonical[leg] for leg in slip.legs}) != len(slip.legs):
            raise ValueError(f"Slip {slip.name} lists the same matched outcome more than once")
        probabilities = [fair_probabilities[leg] for leg in slip.legs]
        kinds = [(resolved[leg]["Pinnacle"] if resolved[leg]["Pinnacle"] and resolved[leg]["Pinnacle"].probability is not None
                  else resolved[leg]["Bovada"]).kind for leg in slip.legs]
        quoted_prices = [max(fair_probabilities[leg], resolved[leg]["Kalshi"].price)
                         if resolved[leg]["Kalshi"] and resolved[leg]["Kalshi"].price is not None
                         else fair_probabilities[leg] for leg in slip.legs]
        chance = 1.0
        for probability in probabilities:
            chance *= probability
        payout = combo_payout(slip.stake, probabilities, kinds, quoted_prices)
        profit = payout - slip.stake
        asks = [resolved[leg]["Kalshi"].price if resolved[leg]["Kalshi"] else None for leg in slip.legs]
        rating = value_rating(slip.stake, payout, probabilities, asks)
        dated_starts = [(start, estimated) for leg in slip.legs for start, estimated in [starts[leg]] if start]
        earliest = min(dated_starts, key=lambda item: item[0]) if dated_starts else None
        buy_by = earliest[0].astimezone(PACIFIC) - timedelta(minutes=5) if earliest else None
        buy_by_label = buy_by.strftime("%Y-%m-%d %I:%M %p PT") if buy_by else "unknown"
        if earliest and earliest[1]:
            buy_by_label += " (estimate from Kalshi)"
        if buy_by and buy_by < now:
            buy_by_label += " (passed)"
        leg_label = ", ".join(f"{leg} {percent(fair_probabilities[leg])}" for leg in slip.legs)
        slip_rows.append([
            slip.name,
            leg_label,
            money(slip.stake),
            percent(chance),
            money(payout),
            money(profit),
            buy_by_label,
            f"{rating}/100",
        ])
        simulated.append(SimulatedSlip(tuple(canonical[leg] for leg in slip.legs), slip.stake, payout))
        total_profit += profit
    slip_rows.append(["TOTAL (all hit)", "", money(total_stake), "", "", "N/A" if missing_fair else money(total_profit), "", ""])
    lines.append(table(["Slip", "Legs (fair)", "Stake", "Hit chance", "Est. payout", "Profit", "Buy by", "Rating"], slip_rows))
    if missing_fair:
        no_line = sorted(leg for leg in missing_fair if leg not in excluded or excluded[leg] == "no matched Kalshi event")
        if no_line:
            lines.extend(["", f"Monte Carlo unavailable. No fair line for: {', '.join(no_line)}."])
        else:
            lines.extend(["", "Monte Carlo unavailable. One or more legs are excluded from pre-match pricing."])
    else:
        result = simulate(simulated, simulation_probabilities, trials, seed)
        lines.extend([
            "",
            f"Monte Carlo | {trials:,} trials | independent distinct legs, shared legs sampled once",
            f"Any profit {percent(result.any_profit)} | +$500 {percent(result.plus_500)} | +$1,000 {percent(result.plus_1000)} | +$2,000 {percent(result.plus_2000)} | median {money(result.median)}",
        ])
    lines.extend(["", "Leg exposure"])
    exposure = defaultdict(lambda: ["", 0.0, 0])
    for slip in slips:
        for leg in slip.legs:
            key = canonical.get(leg, leg.casefold())
            exposure[key][0] = exposure[key][0] or leg
            exposure[key][1] += slip.stake
            exposure[key][2] += 1
    lines.append(table(["Leg", "Stake riding", "Slips"], [[value[0], money(value[1]), str(value[2])] for value in exposure.values()]))
    lines.extend(["", "Payouts use estimated combo cuts, not live Kalshi RFQ quotes. Rating measures estimated value against fair payout and Kalshi asks."])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fairmark")
    commands = parser.add_subparsers(dest="command", required=True)
    slate = commands.add_parser("slate", help="Price a slate of slips")
    slate.add_argument("file", type=Path, help="Text file with slip NAME STAKE: Leg, Leg rows")
    slate.add_argument("--trials", type=int, default=100_000)
    slate.add_argument("--seed", type=int, default=17)
    args = parser.parse_args(argv)
    try:
        slips = read_slips(args.file)
        legs = unique_legs(slips)
        by_source, warnings = fetch_quotes(legs)
        resolved, notes = resolve(legs, by_source, slips)
        for warning in warnings:
            print(f"Data warning: {warning}", file=sys.stderr)
        for note in notes:
            print(note)
        print(render(slips, resolved, args.trials, args.seed))
        return 0
    except (OSError, ValueError) as exc:
        print(f"fairmark slate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
