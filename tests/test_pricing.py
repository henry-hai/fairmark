import pytest

from fairmark.pricing import SimulatedSlip, combo_cut, combo_payout, implied_probability, remove_vig, simulate, value_rating


def test_vig_removal_uses_all_three_soccer_outcomes():
    fair = remove_vig([-110, +250, +300])
    assert sum(fair) == pytest.approx(1)
    assert fair[0] == pytest.approx((110 / 210) / (110 / 210 + 100 / 350 + 100 / 400))
    assert fair[0] != pytest.approx((110 / 210) / (110 / 210 + 100 / 350))
    assert implied_probability("EVEN") == pytest.approx(0.5)


def test_combo_payout_and_heuristic_cuts():
    assert combo_cut([0.7, 0.6], ["winner", "winner"]) == 0.07
    assert combo_cut([0.9, 0.6], ["winner", "winner"]) == 0.10
    assert combo_cut([0.8, 0.6], ["winner", "winner"], [0.89, 0.6]) == 0.10
    assert combo_cut([0.6] * 3, ["winner"] * 3) == 0.08
    assert combo_cut([0.6] * 4, ["winner"] * 4) == 0.11
    assert combo_cut([0.6] * 4, ["prop"] + ["winner"] * 3) == 0.13
    assert combo_cut([0.6] * 7, ["winner"] * 7) == 0.17
    assert combo_payout(100, [0.7, 0.6], ["winner", "winner"]) == pytest.approx(100 * 0.93 / 0.42)


def test_value_rating_uses_payout_discount_and_each_ask_edge():
    fair_payout = 50 / (0.6 * 0.5)
    assert value_rating(50, fair_payout, [0.6, 0.5], [0.6, 0.5]) == 85
    assert value_rating(50, fair_payout * 0.9, [0.6, 0.5], [0.65, 0.52]) == 51
    assert value_rating(50, fair_payout * 0.9, [0.6, 0.5], [0.55, 0.48]) == 79
    assert value_rating(50, fair_payout, [0.6, 0.5], [0.1, 0.1]) == 100
    assert value_rating(50, 0, [0.6, 0.5], [1, 1]) == 0


def test_simulation_reuses_a_shared_leg_and_seed():
    slips = [
        SimulatedSlip(("A", "B"), 100, 200),
        SimulatedSlip(("A", "C"), 100, 300),
    ]
    probabilities = {"A": 0.5, "B": 0.5, "C": 0.5}
    result = simulate(slips, probabilities, trials=10_000, seed=17)
    assert result == simulate(slips, probabilities, trials=10_000, seed=17)
    assert result.any_profit == pytest.approx(0.25, abs=0.015)
    assert result.plus_500 == 0
    assert result.plus_1000 == 0
    assert result.plus_2000 == 0
    assert result.median == -200
