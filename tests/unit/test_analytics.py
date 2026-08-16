from pathlib import Path
from uuid import uuid4

from westbusan.analytics.build import RegionMetrics, _monthly_native_sum, policy_signals
from westbusan.db import Database


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


def test_policy_signals_are_not_emitted_for_high_pressure_with_high_supply() -> None:
    """Catches a supply-review conclusion when the supply evidence contradicts it."""
    metrics = RegionMetrics("west", 12, 0.75, 0.60, 950, "high", "high")

    assert {signal.code for signal in policy_signals(metrics)} == {"RENOVATION_SUPPORT"}


def test_monthly_native_metrics_sum_daily_visitors_but_never_select_other_consumption_codes(
    tmp_path: Path,
) -> None:
    """Catches treating daily visitor rows as incompatible or using non-lodging consumption."""
    db = Database(tmp_path / "analytics.duckdb", Path("sql"))
    db.migrate()
    artifact = uuid4()
    run_id = uuid4()
    for period, code, value in (
        ("2026-01-01", "locgo_regn_visitr_dd_list.visitor_count", 100),
        ("2026-01-02", "locgo_regn_visitr_dd_list.visitor_count", 120),
        ("2026-01", "area_tar_svc_dem_list.1105", 999),
        ("2026-01", "area_tar_svc_dem_list.1107", 250),
    ):
        source_id = "tourism_data_lab" if "visitor" in code else "area_tourism_consumption"
        unit = "count" if "visitor" in code else "KRW"
        db.connection.execute(
            """insert into fact_tourism_demand (
                source_id, metric_code, period, district, region_group, dimension_json,
                dimension_json_hash, source_revision, metric_value, unit,
                source_payload_json, artifact_id, loaded_run_id
            ) values (?, ?, ?, '사하구', 'west', '{}', ?, 'r', ?, ?, '{}', ?, ?)""",
            [source_id, code, period, str(uuid4()), value, unit, artifact, run_id],
        )

    assert _monthly_native_sum(
        db, run_id, "fact_tourism_demand", "사하구", "2026-01", "tourism_data_lab",
        "locgo_regn_visitr_dd_list.visitor_count", "count",
    )[0] == 220
    assert _monthly_native_sum(
        db, run_id, "fact_tourism_demand", "사하구", "2026-01", "area_tourism_consumption",
        "area_tar_svc_dem_list.1107", "KRW",
    )[0] == 250
