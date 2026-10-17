"""Pure pricing and slate simulation functions."""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass


def implied_probability(american_odds: int | str) -> float:
    odds = int(str(american_odds).replace("+", "").replace("EVEN", "100"))
    if odds == 0:
        raise ValueError("American odds cannot be zero")
    return 100 / (odds + 100) if odds > 0 else -odds / (100 - odds)


def remove_vig(american_odds: list[int | str]) -> list[float]:
    if len(american_odds) < 2:
        raise ValueError("A market needs at least two outcomes")
    raw = [implied_probability(odds) for odds in american_odds]
    total = sum(raw)
    return [probability / total for probability in raw]


def combo_cut(probabilities: list[float], kinds: list[str], quoted_prices: list[float] | None = None) -> float:
    count = len(probabilities)
    if count == 0 or len(kinds) != count:
        raise ValueError("Each leg needs a probability and kind")
    if quoted_prices is not None and len(quoted_prices) != count:
        raise ValueError("Each leg needs a quoted price")
    if count == 1:
        return 0.0
    if count == 2:
        cut = 0.07
    elif count == 3:
        cut = 0.08
    elif count < 7:
        cut = 0.11
    else:
        cut = 0.17
    if max(quoted_prices or probabilities) >= 0.88 or any(kind == "prop" for kind in kinds):
        cut = max(cut, 0.10 if count < 4 else 0.13)
    return cut


def combo_payout(
    stake: float, probabilities: list[float], kinds: list[str], quoted_prices: list[float] | None = None
) -> float:
    if stake <= 0 or any(not 0 < probability <= 1 for probability in probabilities):
        raise ValueError("Stake and probabilities must be positive")
    hit_chance = 1.0
    for probability in probabilities:
        hit_chance *= probability
    return stake * (1 - combo_cut(probabilities, kinds, quoted_prices)) / hit_chance


def value_rating(stake: float, payout: float, probabilities: list[float], asks: list[float | None]) -> int:
    if len(probabilities) != len(asks):
        raise ValueError("Each leg needs an ask price")
    fair_payout = stake
    for probability in probabilities:
        fair_payout /= probability
    # Start at 85, move 2 points per percentage point of payout discount or Kalshi ask edge.
    score = 85 - 200 * max(0, 1 - payout / fair_payout)
    score += 200 * sum(fair - ask for fair, ask in zip(probabilities, asks, strict=True) if ask is not None)
    return round(max(0, min(100, score)))


@dataclass(frozen=True)
class SimulatedSlip:
    legs: tuple[str, ...]
    stake: float
    payout: float


@dataclass(frozen=True)
class Simulation:
    any_profit: float
    plus_500: float
    plus_1000: float
    plus_2000: float
    median: float


def simulate(
    slips: list[SimulatedSlip],
    probabilities: dict[str, float],
    trials: int = 100_000,
    seed: int = 17,
) -> Simulation:
    if trials < 1:
        raise ValueError("Trials must be positive")
    if any(not 0 <= probability <= 1 for probability in probabilities.values()):
        raise ValueError("Probabilities must be between zero and one")
    if any(leg not in probabilities for slip in slips for leg in slip.legs):
        raise ValueError("Every simulated leg needs a probability")
    if not slips:
        raise ValueError("At least one slip is required")
    rng = random.Random(seed)
    total_stake = sum(slip.stake for slip in slips)
    profits = []
    for _ in range(trials):
        hits = {leg: rng.random() < probability for leg, probability in probabilities.items()}
        profit = -total_stake
        for slip in slips:
            if all(hits[leg] for leg in slip.legs):
                profit += slip.payout
        profits.append(profit)
    return Simulation(
        sum(value > 0 for value in profits) / trials,
        sum(value >= 500 for value in profits) / trials,
        sum(value >= 1000 for value in profits) / trials,
        sum(value >= 2000 for value in profits) / trials,
        statistics.median(profits),
    )
