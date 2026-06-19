from calc.listing_event_monitor import calculate_strategy_tier_from_volumes


def test_calculate_strategy_tier_from_volumes():
    assert calculate_strategy_tier_from_volumes(10_000_000, 5_000_000) == 'A'
    assert calculate_strategy_tier_from_volumes(56_800_000, 4_370_000) == 'B'
    assert calculate_strategy_tier_from_volumes(1_000_000, 500_000) == 'B'
    assert calculate_strategy_tier_from_volumes(999_999, 500_000) == 'C'
    assert calculate_strategy_tier_from_volumes(1_000_000, 499_999) == 'C'
