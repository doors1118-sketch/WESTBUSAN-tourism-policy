from westbusan.analytics.build import RegionMetrics, policy_signals


def test_old_small_high_pressure_region_gets_renovation_and_supply_signals() -> None:
    """Catches policy conclusions that ignore the required evidence combination."""
    metrics = RegionMetrics(
        region_group="west",
        median_rooms=12,
        small_facility_share=0.75,
        building_30y_share=0.60,
        visitors_per_100_rooms=950,
        demand_pressure_band="high",
        room_supply_band="low",
    )

    signals = policy_signals(metrics)

    assert {signal.code for signal in signals} == {
        "RENOVATION_SUPPORT",
        "SUPPLY_EXPANSION_REVIEW",
    }
    assert all(signal.evidence_json for signal in signals)


def test_incomplete_or_contradictory_evidence_emits_no_forced_signal() -> None:
    """Catches emitting a desired policy despite missing or conflicting evidence."""
    metrics = RegionMetrics(
        region_group="west",
        median_rooms=None,
        small_facility_share=None,
        building_30y_share=0.60,
        visitors_per_100_rooms=None,
        demand_pressure_band="unclassified",
        room_supply_band="high",
    )

    assert policy_signals(metrics) == []
