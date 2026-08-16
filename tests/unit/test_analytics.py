from pathlib import Path
from uuid import uuid4

from westbusan.analytics.build import (
    RegionMetrics,
    _comparison_quality,
    _group_distribution,
    _group_pressure,
    _growth_evidence,
    _monthly_native_sum,
    _period_metric_set,
    _replace_group_regions,
    _replace_signals,
    _visible_run_ids,
    policy_signals,
)
from westbusan.config import PolicyConfig
from westbusan.db import Database


def test_old_small_high_pressure_region_gets_renovation_and_supply_signals() -> None:
    """Catches renovation depending on demand or supply expansion depending on age."""
    metrics = RegionMetrics(
        region_group="west",
        median_rooms=12,
        small_facility_share=0.75,
        building_old_share=0.60,
        visitor_person_days_per_100_rooms=950,
        demand_pressure_band="high",
        supply_stock_band="low",
        room_supply_growth_band="high",
        visitor_growth_minus_room_supply_growth=None,
        tourism_registration_room_share=None,
        openings=None,
        closures=None,
        evidence=_policy_evidence(
            "median_rooms",
            "small_facility_share",
            "building_old_share",
            "visitor_person_days_per_100_rooms",
            "supply_stock_band",
        ),
    )

    signals = policy_signals(metrics)

    assert {signal.code for signal in signals if signal.status == "triggered"} == {
        "RENOVATION_SUPPORT",
        "SUPPLY_EXPANSION_REVIEW",
    }
    assert len(signals) == 5
    assert all(signal.evidence_json for signal in signals)


def test_incomplete_policy_matrix_is_explicitly_unavailable() -> None:
    """Catches silently omitting a policy rule whose evidence is missing."""
    metrics = RegionMetrics(
        region_group="west",
        median_rooms=None,
        small_facility_share=None,
        building_old_share=0.60,
        visitor_person_days_per_100_rooms=None,
        demand_pressure_band="unclassified",
        supply_stock_band="high",
        room_supply_growth_band="high",
        visitor_growth_minus_room_supply_growth=None,
        tourism_registration_room_share=None,
        openings=None,
        closures=None,
        evidence=_policy_evidence("building_old_share"),
    )

    signals = policy_signals(metrics)

    assert len(signals) == 5
    assert {signal.status for signal in signals} == {"unavailable"}


def test_policy_signals_are_not_emitted_for_high_pressure_with_high_supply() -> None:
    """Catches a supply-review conclusion when the supply evidence contradicts it."""
    metrics = RegionMetrics(
        "west", 12, 0.75, 0.60, 950, "high", "high", "high", None, None,
        None, None,
        _policy_evidence(
            "median_rooms", "small_facility_share", "building_old_share",
            "visitor_person_days_per_100_rooms", "supply_stock_band",
        ),
    )

    statuses = {signal.code: signal.status for signal in policy_signals(metrics)}
    assert statuses["RENOVATION_SUPPORT"] == "triggered"
    assert statuses["SUPPLY_EXPANSION_REVIEW"] == "not_triggered"


def test_policy_matrix_includes_old_low_demand_growth_low_tourism_and_exit_rules() -> None:
    """Catches three required evidence combinations disappearing from the matrix."""
    metrics = RegionMetrics(
        "west", 30, 0.2, 0.7, 100, "low", "medium", "low", 0.25, 0.1,
        1, 5,
        _policy_evidence(
            "building_old_share", "visitor_person_days_per_100_rooms",
            "visitor_growth_minus_room_supply_growth",
            "tourism_registration_room_share", "openings", "closures",
        ),
    )

    triggered = {
        signal.code for signal in policy_signals(metrics) if signal.status == "triggered"
    }

    assert triggered == {
        "OLD_LOW_DEMAND_REPOSITIONING",
        "DEMAND_GROWTH_LOW_TOURISM_CAPACITY",
        "CLOSURE_DOMINANT_MARKET_STABILIZATION",
    }


def _policy_evidence(*names: str) -> dict[str, dict[str, float]]:
    return {
        name: {"numerator": 1.0, "denominator": 1.0, "coverage": 1.0}
        for name in names
    }


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


def test_visible_runs_use_immutable_lineage_not_creation_order_or_run_status(
    tmp_path: Path,
) -> None:
    """Catches a later RUNNING/BLOCKED run leaking into an earlier observation set."""
    db = Database(tmp_path / "visibility.duckdb", Path("sql"))
    db.migrate()
    first, second = uuid4(), uuid4()
    db.connection.execute(
        "insert into pipeline_run (run_id, mode, started_at, status, business_date) values (?, 'test', '2026-01-10', 'PUBLISHED', '2026-01-10')",
        [first],
    )
    db.connection.execute(
        "insert into pipeline_run (run_id, mode, started_at, status, business_date) values (?, 'test', '2026-02-10', 'BLOCKED', '2026-02-10')",
        [second],
    )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?), (?, ?), (?, ?)",
        [first, first, second, first, second, second],
    )

    assert _visible_run_ids(db, first) == (first,)
    assert _visible_run_ids(db, second) == (first, second)


def test_monthly_metric_uses_recollection_membership_not_first_loader(
    tmp_path: Path,
) -> None:
    """A successful recollection can use an identical fact first inserted by BLOCKED."""
    db = Database(tmp_path / "fact-membership.duckdb", Path("sql"))
    db.migrate()
    blocked, successful = uuid4(), uuid4()
    for run_id, status in ((blocked, "BLOCKED"), (successful, "RUNNING")):
        db.connection.execute(
            """insert into pipeline_run (
                   run_id, mode, started_at, status, business_date
               ) values (?, 'daily', now(), ?, '2026-08-16')""",
            [run_id, status],
        )
    db.connection.execute(
        "insert into pipeline_run_input (run_id, input_run_id) values (?, ?)",
        [successful, successful],
    )
    db.connection.execute(
        """insert into fact_tourism_demand (
               source_id, metric_code, period, district, region_group,
               dimension_json, dimension_json_hash, source_revision,
               metric_value, unit, source_payload_json, artifact_id,
               loaded_run_id, observation_key
           ) values (
               'tourism_data_lab', 'locgo_regn_visitr_dd_list.visitor_count',
               '2026-08-01', '사하구', 'west', '{}', 'dimension', 'revision',
               123, 'count', '{}', ?, ?, 'observation'
           )""",
        [uuid4(), blocked],
    )
    db.connection.execute(
        """insert into run_fact_observation (run_id, family, observation_key)
           values (?, 'tourism', 'observation')""",
        [successful],
    )

    assert _monthly_native_sum(
        db,
        successful,
        "fact_tourism_demand",
        "사하구",
        "2026-08",
        "tourism_data_lab",
        "locgo_regn_visitr_dd_list.visitor_count",
        "count",
    )[0] == 123


def test_period_metric_set_keeps_historical_unknown_rooms_in_coverage() -> None:
    """Catches substituting current inventory for a month with one known of two facilities."""
    metrics = _period_metric_set([10.0, None], small_room_threshold=20)

    assert metrics["room_sum"] == 10.0
    assert metrics["facilities"] == 2
    assert metrics["coverage"] == 0.5


def test_growth_evidence_value_matches_the_stored_growth_gap() -> None:
    """Catches evidence whose numerator describes visitor growth instead of the gap."""
    evidence = _growth_evidence(0.25, 100.0, 80.0, 100.0, 100.0, 0.8)

    assert evidence["value"] == 0.25
    assert evidence["numerator"] == 0.25
    assert evidence["denominator"] == 1.0


def test_group_pressure_does_not_sum_district_person_day_estimates() -> None:
    """Catches district visitor estimates being presented as a group headcount."""
    assert _group_pressure([(100.0, 100.0), (100.0, 100.0)]) is None
    assert _group_pressure([(100.0, 100.0)]) == 100.0


def test_sparse_daily_visitor_numerator_has_separate_expected_day_coverage(
    tmp_path: Path,
) -> None:
    """Catches two observed days being treated as a complete monthly visitor numerator."""
    db = Database(tmp_path / "sparse-visitors.duckdb", Path("sql")); db.migrate()
    run_id, artifact = uuid4(), uuid4()
    for day, value in (("2026-01-01", 100), ("2026-01-02", 120)):
        db.connection.execute(
            """
            insert into fact_tourism_demand (
                source_id, metric_code, period, district, region_group,
                dimension_json, dimension_json_hash, source_revision, metric_value,
                unit, source_payload_json, artifact_id, loaded_run_id
            ) values ('tourism_data_lab',
                      'locgo_regn_visitr_dd_list.visitor_count', ?, '사하구',
                      'west', '{}', ?, 'r', ?, 'count', '{}', ?, ?)
            """,
            [day, day, value, artifact, run_id],
        )

    value, _, coverage = _monthly_native_sum(
        db,
        run_id,
        "fact_tourism_demand",
        "사하구",
        "2026-01",
        "tourism_data_lab",
        "locgo_regn_visitr_dd_list.visitor_count",
        "count",
    )

    assert value == 220
    assert coverage == {
        "expected_days": 31,
        "observed_days": 2,
        "day_coverage": 2 / 31,
        "source_coverage": 1.0,
        "dimension_coverage": 1.0,
        "geography_coverage": 1.0,
        "overall": 2 / 31,
    }


def test_division_quality_warns_for_partial_coverage() -> None:
    """Catches reporting a 0.5-covered West/East division as good evidence."""
    assert _comparison_quality([1.0, 0.5]) == "warning"


def test_group_distribution_flattens_facility_values_not_district_medians() -> None:
    """Catches a 1/40 facility being inflated into a 50% regional share."""
    median_rooms, small_share, age30_share = _group_distribution(
        [1.0, *([9.0] * 9)], [40.0, *([20.0] * 9)], 1, 30
    )

    assert (median_rooms, small_share, age30_share) == (9.0, 0.1, 0.1)


def test_group_old_share_uses_configured_threshold() -> None:
    """Catches silently reverting a configured 25-year rule to 30 years."""
    assert _group_distribution([10.0], [26.0, 20.0], 20, 25)[2] == 0.5


def test_partial_group_stock_and_rooms_are_unavailable(tmp_path: Path) -> None:
    """Catches a subset of West districts being published as the group total."""
    db = Database(tmp_path / "partial-group.duckdb", Path("sql")); db.migrate()
    run_id = uuid4()
    rows = [
        {
            "district": "사하구",
            "group": "west",
            "period": "current",
            "values": {
                "stock_observed": True,
                "facilities": 1,
                "registrations": 1,
                "known": 1,
                "room_values": [10.0],
                "age_known": 0,
            },
        }
    ]

    _replace_group_regions(db, run_id, rows)

    assert db.query(
        """select district_count, observed_district_count,
                  physical_facility_count, room_sum, room_known_facility_count
             from mart_region_group_month"""
    ) == [(4, 1, None, None, 0)]


def test_partial_group_cannot_trigger_any_policy_rule(tmp_path: Path) -> None:
    db = Database(tmp_path / "partial-policy.duckdb", Path("sql")); db.migrate()
    run_id = uuid4()
    rows = [
        {
            "district": "사하구",
            "group": "west",
            "period": "current",
            "values": {
                "facilities": 1,
                "coverage": 1.0,
                "room_values": [5.0],
                "age_values": [40.0],
                "tourism_room_coverage": 1.0,
                "tourism_rooms": 0.0,
            },
            "evidence": {
                "visitor_person_days_per_100_rooms": {
                    "numerator": 1000.0,
                    "denominator": 5.0,
                    "coverage": 1.0,
                }
            },
            "openings": 0,
            "closures": 10,
            "growth_gap": 1.0,
            "stock_band": "low",
            "pressure": "high",
            "supply": "low",
        }
    ]

    _replace_signals(
        db,
        run_id,
        rows,
        [],
        PolicyConfig(small_room_threshold=20, old_building_years=[20, 30]),
    )

    assert db.query(
        "select distinct evaluation_status from mart_policy_signal"
    ) == [("unavailable",)]
    assert db.query("select count(*) from mart_policy_signal") == [(5,)]


def test_period_metric_set_retains_known_room_total_for_tourism_denominator() -> None:
    """A period-local tourism share must divide by that period's known rooms."""
    metrics = _period_metric_set([10.0, None], small_room_threshold=20)

    assert metrics["room_sum"] == 10.0
